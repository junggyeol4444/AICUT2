from __future__ import annotations

import json
import hashlib
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .database import Database
from .discovery import run_content_discovery
from .audio import run_audio_analyzer
from .media import check_disk_capacity, probe_media
from .longterm import run_window_understanding
from .multimodal import analyze_audio_tracks, analyze_precision_ranges, analyze_video
from .package import build_episode_package, run_packaging_model
from .producer import run_producer
from .retrieval import run_scene_retrieval
from .render import LoudnessTarget, RenderPlan, render
from .processes import ProcessSupervisor
from .planning import run_dynamic_planner
from .pacing import run_smart_pacing
from .stt import transcribe_range, transcribe_tracks
from .understanding import PreprocessPlan, build_scan_plan, execute_preprocess, select_precision_ranges
from .vision import run_vision_analyzer


class PipelineCancelled(RuntimeError):
    pass


class PipelineManager:
    """Checkpointed orchestration for the local, long-running analysis stages."""

    def __init__(
        self, database: Database, max_workers: int = 1, *,
        probe: Callable = probe_media, preprocess: Callable = execute_preprocess,
        check_disk: Callable = check_disk_capacity,
        transcribe: Callable = transcribe_tracks,
        transcribe_chunk: Callable = transcribe_range,
        analyze_audio: Callable = analyze_audio_tracks,
        analyze_external_audio: Callable = run_audio_analyzer,
        analyze_vision: Callable = analyze_video,
        analyze_precision: Callable = analyze_precision_ranges,
        analyze_external_vision: Callable = run_vision_analyzer,
        understand_window: Callable = run_window_understanding,
        discover: Callable = run_content_discovery,
        retrieve: Callable = run_scene_retrieval,
        plan: Callable = run_dynamic_planner,
        pace: Callable = run_smart_pacing,
        render_episode: Callable = render,
        generate_packages: Callable = run_packaging_model,
        package_episode: Callable = build_episode_package,
        produce: Callable = run_producer,
    ):
        self.database = database
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aicut")
        self.probe = probe
        self.check_disk = check_disk
        self.preprocess = preprocess
        self.transcribe = transcribe
        self.transcribe_chunk = transcribe_chunk
        self.analyze_audio = analyze_audio
        self.analyze_external_audio = analyze_external_audio
        self.analyze_vision = analyze_vision
        self.analyze_precision = analyze_precision
        self.analyze_external_vision = analyze_external_vision
        self.understand_window = understand_window
        self.discover = discover
        self.retrieve = retrieve
        self.plan = plan
        self.pace = pace
        self.render_episode = render_episode
        self.generate_packages = generate_packages
        self.package_episode = package_episode
        self.produce = produce
        self._default_preprocess = preprocess is execute_preprocess
        self._default_transcribe = transcribe is transcribe_tracks
        self._default_transcribe_chunk = transcribe_chunk is transcribe_range
        self._default_audio = analyze_audio is analyze_audio_tracks
        self._default_external_audio = analyze_external_audio is run_audio_analyzer
        self._default_vision = analyze_vision is analyze_video
        self._default_precision = analyze_precision is analyze_precision_ranges
        self._default_external_vision = analyze_external_vision is run_vision_analyzer
        self._default_understand_window = understand_window is run_window_understanding
        self._default_discover = discover is run_content_discovery
        self._default_retrieve = retrieve is run_scene_retrieval
        self._default_plan = plan is run_dynamic_planner
        self._default_pace = pace is run_smart_pacing
        self._default_render = render_episode is render
        self._default_generate_packages = generate_packages is run_packaging_model
        self._default_package_episode = package_episode is build_episode_package
        self._default_producer = produce is run_producer
        self.processes = ProcessSupervisor()
        self._jobs: dict[str, Future] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._hash_context = threading.local()
        self._retry_context = threading.local()

    def submit(
        self, project_id: str, manifest_path: str | None = None, *,
        options: dict[str, Any] | None = None, resume: bool = True,
    ) -> bool:
        configuration = dict(options or {})
        if manifest_path:
            configuration["manifest_path"] = manifest_path
        with self._lock:
            running = self._jobs.get(project_id)
            if running and not running.done():
                return False
            cancel = threading.Event()
            self._cancel[project_id] = cancel
            self._jobs[project_id] = self.executor.submit(self._run, project_id, configuration, resume, cancel)
            return True

    def cancel(self, project_id: str) -> bool:
        with self._lock:
            event = self._cancel.get(project_id)
            future = self._jobs.get(project_id)
        if not event or not future or future.done():
            return False
        event.set()
        self.processes.cancel(project_id)
        return True

    def state(self, project_id: str) -> dict:
        with self._lock:
            future = self._jobs.get(project_id)
            cancelling = bool(self._cancel.get(project_id) and self._cancel[project_id].is_set())
        return {
            "project_id": project_id, "running": bool(future and not future.done()),
            "done": bool(future and future.done()),
            "failed": bool(future and future.done() and future.exception()),
            "cancelling": cancelling, "active_pids": self.processes.pids(project_id),
            "steps": self.database.pipeline_steps(project_id),
        }

    def _run(self, project_id: str, options: dict[str, Any], resume: bool, cancel: threading.Event) -> None:
        try:
            completed = {item["step"]: item for item in self.database.pipeline_steps(project_id) if item["status"] == "COMPLETE"}
            project = self.database.get_project(project_id)
            options = self._calibrated_options(project, options)
            input_hash = self._input_hash(project["file_path"], options)
            self._retry_context.value = self._validate_retry_policy(options.get("retry_policy") or {})
            runner = self.processes.runner(project_id, cancel)
            media = self._step(project_id, "PROBE", "PARSING", 5, 15, cancel, resume, completed,
                               lambda: self.probe(project["file_path"]).to_dict(), input_hash)
            self.database.set_media_info(project_id, media)

            artifact_root = Path(options.get("output_directory") or Path("artifacts") / project_id).expanduser().resolve()
            if options.get("disk_check"):
                required = int(options["disk_required_bytes"])
                disk = self._step(
                    project_id, "DISK_CHECK", "PARSING", 16, 17, cancel, resume, completed,
                    lambda: self.check_disk(artifact_root, required, int(options.get("disk_reserve_bytes", 0))),
                )
                if disk["available_bytes"] < required:
                    raise RuntimeError("디스크 용량 검사 결과가 올바르지 않습니다.")
            if options.get("preprocess", False):
                result = self._step(
                    project_id, "PREPROCESS", "PARSING", 18, 35, cancel, resume, completed,
                    lambda: self._invoke(self.preprocess, self._default_preprocess, runner, PreprocessPlan(
                        project["file_path"], str(artifact_root), int(media["audio_tracks"]),
                        float(options["frame_interval_sec"]),
                    )),
                )
                self.database.add_artifacts(project_id, [
                    {"kind": item["kind"], "path": item["path"], "metadata": {"command": item["command"]}}
                    for item in result["artifacts"]
                ])

            windows = self._step(
                project_id, "SCAN_PLAN", "UNDERSTANDING", 38, 42, cancel, resume, completed,
                lambda: {"windows": [window.__dict__ for window in build_scan_plan(
                    float(media["duration_sec"]), float(options.get("coarse_window_sec", 300)),
                    options.get("precision_ranges"),
                )]},
            )
            self.database.replace_scan_windows(project_id, windows["windows"])

            audio_paths = options.get("audio_paths") or [
                str(artifact_root / f"audio-track-{index:02d}.wav") for index in range(int(media["audio_tracks"]))
            ]
            if options.get("audio_analysis") and audio_paths:
                audio_observations = []
                audio_executable = options.get("audio_executable")
                chunks = self._analysis_chunks(
                    float(media["duration_sec"]),
                    options.get("analysis_chunk_sec") if (self._default_audio or audio_executable) else None,
                )
                for index, chunk in enumerate(chunks):
                    step = "AUDIO_ANALYSIS" if len(chunks) == 1 else f"AUDIO_ANALYSIS_{index:06d}"
                    progress = self._chunk_progress(43, 50, index, len(chunks))
                    audio = self._step(
                        project_id, step, "UNDERSTANDING", progress[0], progress[1], cancel, resume, completed,
                        lambda chunk=chunk: self._analyze_audio_chunk(
                            runner, audio_paths, float(media["duration_sec"]), float(options.get("audio_window_sec", 1.0)),
                            chunk, audio_executable, artifact_root / "audio-analysis" / f"chunk-{index:06d}.json",
                        ),
                    )
                    audio_observations.extend(audio["observations"])
                self.database.replace_observations(project_id, "AUDIO", audio_observations)

            if options.get("vision_analysis"):
                vision_observations = []
                vision_executable = options.get("vision_executable")
                chunks = self._analysis_chunks(
                    float(media["duration_sec"]),
                    options.get("analysis_chunk_sec") if (self._default_vision or vision_executable) else None,
                )
                for index, chunk in enumerate(chunks):
                    step = "VISION_ANALYSIS" if len(chunks) == 1 else f"VISION_ANALYSIS_{index:06d}"
                    progress = self._chunk_progress(51, 58, index, len(chunks))
                    vision = self._step(
                        project_id, step, "UNDERSTANDING", progress[0], progress[1], cancel, resume, completed,
                        lambda chunk=chunk: self._analyze_vision_chunk(
                            runner, project["file_path"], float(media["duration_sec"]),
                            float(options.get("vision_interval_sec", 5.0)), chunk, vision_executable,
                            artifact_root / "vision" / f"chunk-{index:06d}.json",
                        ),
                    )
                    vision_observations.extend(vision["observations"])
                self.database.replace_observations(project_id, "VISION", vision_observations)

            executable = options.get("stt_executable")
            if executable:
                transcript = []
                chunks = self._analysis_chunks(float(media["duration_sec"]), options.get("stt_chunk_sec"))
                for index, chunk in enumerate(chunks):
                    step = "STT" if len(chunks) == 1 else f"STT_{index:06d}"
                    progress = self._chunk_progress(45, 58, index, len(chunks))
                    stt = self._step(
                        project_id, step, "UNDERSTANDING", progress[0], progress[1], cancel, resume, completed,
                        lambda chunk=chunk, index=index: self._transcribe_chunk(
                            runner, executable, audio_paths, float(media["duration_sec"]),
                            artifact_root / "stt" / f"chunk-{index:06d}", options.get("language"), chunk,
                            len(chunks) > 1,
                        ),
                    )
                    transcript.extend(stt["segments"])
                transcript.sort(key=lambda item: (item["start_sec"], item["track_index"]))
                self.database.replace_transcript(project_id, transcript)

            understanding_ranges = []
            understanding_executable = options.get("understanding_executable")
            if understanding_executable:
                analysis = self.database.analysis_input(project_id)
                coarse = [item for item in analysis["scan_windows"] if item["pass_kind"] == "COARSE"]
                memory, summaries = {}, []
                for index, window in enumerate(coarse):
                    progress = self._chunk_progress(58, 66, index, len(coarse))
                    selected_timeline = [item for item in analysis["timeline"]
                                         if item["end_sec"] > window["start_sec"] and item["start_sec"] < window["end_sec"]]
                    understood = self._step(
                        project_id, f"UNDERSTANDING_{index:06d}", "UNDERSTANDING",
                        progress[0], progress[1], cancel, resume, completed,
                        lambda index=index, window=window, memory=memory, selected_timeline=selected_timeline:
                            self._invoke(
                                self.understand_window, self._default_understand_window, runner,
                                understanding_executable, window, selected_timeline, memory,
                                artifact_root / "understanding" / f"window-{index:06d}.json",
                            ),
                    )
                    memory = understood["memory"]
                    understanding_ranges.extend(understood["precision_ranges"])
                    summaries.append({**window, "summary": understood["summary"], "memory": memory,
                                      "precision_ranges": understood["precision_ranges"]})
                self.database.replace_understanding_windows(project_id, summaries)

            precision_policy = options.get("precision_policy")
            signal_ranges = []
            if precision_policy:
                analysis = self.database.analysis_input(project_id)
                precision = self._step(
                    project_id, "PRECISION_PLAN", "UNDERSTANDING", 67, 69, cancel, resume, completed,
                    lambda: {"ranges": select_precision_ranges(
                        float(media["duration_sec"]), analysis["transcript"], analysis["observations"], precision_policy,
                    )},
                )
                signal_ranges = precision["ranges"]
            selected_ranges = understanding_ranges + signal_ranges
            combined_ranges = list(options.get("precision_ranges") or []) + selected_ranges
            if combined_ranges:
                self.database.replace_scan_windows(project_id, [window.__dict__ for window in build_scan_plan(
                    float(media["duration_sec"]), float(options.get("coarse_window_sec", 300)), combined_ranges,
                )])
            if options.get("precision_analysis") and selected_ranges:
                detailed = self._step(
                    project_id, "PRECISION_ANALYSIS", "UNDERSTANDING", 70, 75, cancel, resume, completed,
                    lambda: self._invoke(self.analyze_precision, self._default_precision, runner,
                        project["file_path"], [path for path in audio_paths if Path(path).is_file()],
                        float(media["duration_sec"]), selected_ranges,
                        audio_window_sec=float(options["precision_audio_window_sec"]),
                        vision_interval_sec=float(options["precision_vision_interval_sec"]),
                    ),
                )
                self.database.replace_precision_observations(project_id, detailed["observations"])

            discovery_executable = options.get("discovery_executable")
            if discovery_executable:
                discovered = self._step(
                    project_id, "CONTENT_DISCOVERY", "DISCOVERING", 76, 82, cancel, resume, completed,
                    lambda: self._invoke(
                        self.discover, self._default_discover, runner,
                        discovery_executable, self.database.analysis_input(project_id),
                        artifact_root / "discovery", float(media["duration_sec"]),
                    ),
                )
                self.database.import_analysis(project_id, discovered["manifest"])

            retrieval_executable = options.get("retrieval_executable")
            if retrieval_executable:
                retrieval_input = self.database.analysis_input(project_id)
                if not retrieval_input["candidates"]:
                    raise ValueError("장면 검색을 실행하려면 콘텐츠 후보가 먼저 필요합니다.")
                retrieved = self._step(
                    project_id, "SCENE_RETRIEVAL", "PLANNING", 83, 87, cancel, resume, completed,
                    lambda: self._invoke(
                        self.retrieve, self._default_retrieve, runner,
                        retrieval_executable, retrieval_input, artifact_root / "retrieval",
                        float(media["duration_sec"]),
                    ),
                )
                self.database.replace_retrieved_scenes(project_id, retrieved["scenes"])

            planner_executable = options.get("planner_executable")
            producer_executable = options.get("producer_executable")
            if planner_executable and producer_executable:
                raise ValueError("planner_executable과 producer_executable은 동시에 사용할 수 없습니다.")
            if planner_executable:
                planned = self._step(
                    project_id, "DYNAMIC_PLANNING", "PLANNING", 88, 94, cancel, resume, completed,
                    lambda: self._invoke(
                        self.plan, self._default_plan, runner,
                        planner_executable, self.database.analysis_input(project_id),
                        artifact_root / "planning", float(media["duration_sec"]),
                    ),
                )
                self.database.save_planning_version(project_id, planned["manifest"])
                self.database.import_analysis(project_id, planned["manifest"])
            manifest_path = options.get("manifest_path")
            if producer_executable:
                produced = self._step(
                    project_id, "AI_PRODUCER", "PLANNING", 88, 94, cancel, resume, completed,
                    lambda: self._invoke(self.produce, self._default_producer, runner,
                                         producer_executable, self.database.analysis_input(project_id),
                                         artifact_root / "producer", float(media["duration_sec"])),
                )
                self.database.import_analysis(project_id, produced["manifest"])
            elif manifest_path and not planner_executable:
                manifest = self._step(
                    project_id, "ANALYSIS_IMPORT", "DISCOVERING", 88, 94, cancel, resume, completed,
                    lambda: json.loads(Path(manifest_path).expanduser().resolve().read_text(encoding="utf-8")),
                )
                self.database.import_analysis(project_id, manifest)
            elif not planner_executable and not discovery_executable and not retrieval_executable:
                self.database.update_status(
                    project_id, "UNDERSTANDING", 58 if executable else 42,
                    "전처리 파이프라인 완료 · 장기 방송 이해 AI 결과를 기다립니다.",
                )
            pacing_executable = options.get("pacing_executable")
            if pacing_executable:
                paced = self._step(
                    project_id, "SMART_PACING", "PLANNING", 95, 97, cancel, resume, completed,
                    lambda: self._invoke(
                        self.pace, self._default_pace, runner,
                        pacing_executable, self.database.analysis_input(project_id), artifact_root / "pacing",
                    ),
                )
                self.database.apply_pacing_decisions(project_id, paced["decisions"])
            if options.get("render"):
                episodes = self.database.list_episodes(project_id)
                if not episodes:
                    raise ValueError("렌더링을 실행하려면 기획된 에피소드가 필요합니다.")
                audio_mix = tuple(options.get("render_audio_mix") or ())
                available_tracks = int(media.get("audio_tracks", 0))
                if any(int(item.get("track_index", -1)) >= available_tracks for item in audio_mix):
                    raise ValueError("render_audio_mix가 원본에 존재하지 않는 오디오 트랙을 참조합니다.")
                render_root = Path(options.get("render_output_directory") or artifact_root / "renders").resolve()
                for index, episode in enumerate(episodes):
                    progress = self._chunk_progress(97, 100, index, len(episodes))
                    episode_id = episode["episode_id"]
                    output_path = render_root / f"{episode_id}.mp4"
                    plan = RenderPlan(
                        project["file_path"], str(output_path), tuple(self.database.get_timeline(episode_id)),
                        width=int(options.get("render_width", 1920)), height=int(options.get("render_height", 1080)),
                        video_codec=str(options.get("video_codec", "libx264")),
                        audio_codec=str(options.get("audio_codec", "aac")),
                        subtitle_path=(options.get("subtitle_paths") or {}).get(episode_id),
                        audio_mix=audio_mix,
                    )
                    target = LoudnessTarget(
                        integrated_lufs=float(options.get("integrated_lufs", -14)),
                        true_peak_db=float(options.get("true_peak_db", -1)),
                        loudness_range=float(options.get("loudness_range", 11)),
                    )
                    self.database.set_render_status(episode_id, "RUNNING")
                    try:
                        rendered = self._step(
                            project_id, f"RENDER_{episode_id}", "RENDERING",
                            progress[0], progress[1], cancel, resume, completed,
                            lambda plan=plan, target=target: self._invoke(
                                self.render_episode, self._default_render, runner,
                                plan, loudness_target=target,
                            ),
                        )
                    except Exception:
                        self.database.set_render_status(episode_id, "FAILED")
                        raise
                    self.database.set_render_status(episode_id, "COMPLETE", rendered["output_path"])
            packaging_executable = options.get("packaging_executable")
            if packaging_executable:
                episodes = self.database.list_episodes(project_id)
                missing_outputs = [item["episode_id"] for item in episodes
                                   if not item.get("output_mp4_path") or not Path(item["output_mp4_path"]).is_file()]
                if missing_outputs:
                    raise ValueError("패키징 전에 모든 에피소드 렌더가 필요합니다: " + ", ".join(missing_outputs))
                packaging = self._step(
                    project_id, "PACKAGING_PLAN", "PACKAGED", 98, 99, cancel, resume, completed,
                    lambda: self._invoke(
                        self.generate_packages, self._default_generate_packages, runner,
                        packaging_executable, self.database.analysis_input(project_id), artifact_root / "packaging",
                    ),
                )
                package_root = Path(options.get("package_output_directory") or artifact_root / "packages").resolve()
                for index, item in enumerate(packaging["packages"]):
                    episode_id = item["episode_id"]
                    episode = self.database.get_episode(episode_id)
                    output = package_root / episode_id
                    packaged = self._step(
                        project_id, f"PACKAGE_{episode_id}", "PACKAGED",
                        *self._chunk_progress(99, 100, index, len(packaging["packages"])), cancel, resume, completed,
                        lambda item=item, episode=episode, output=output: self._invoke(
                            self.package_episode, self._default_package_episode, runner,
                            item["metadata"], episode["output_mp4_path"], item["thumbnail_timestamps"], output,
                        ),
                    )
                    thumbnails = packaged.get("thumbnails", [])
                    self.database.set_episode_package(
                        episode_id, item["metadata"], thumbnails[0] if thumbnails else None,
                    )
            if options.get("render") or packaging_executable:
                self.database.update_status(project_id, "REVIEW_PENDING", 100,
                                            "영상과 패키징 준비 완료 · 사람 검수를 기다립니다.")
        except PipelineCancelled:
            self.database.update_status(project_id, "QUEUED", 0, "사용자 요청으로 분석을 취소했습니다. 재개할 수 있습니다.")
        except Exception as error:
            self.database.fail_project(project_id, str(error))

    def _step(self, project_id, step, stage, start, end, cancel, resume, completed, operation, input_hash=None):
        self._check_cancel(cancel)
        input_hash = input_hash or self._hash_context.value
        if resume and step in completed and self._checkpoint_usable(step, completed[step], input_hash):
            self.database.update_status(project_id, stage, end, f"{step} 체크포인트를 재사용합니다.")
            return completed[step]["output"]
        retry = self._retry_context.value.get(step, {"max_attempts": 1, "backoff_sec": 0.0})
        output = None
        for attempt in range(1, retry["max_attempts"] + 1):
            self.database.save_pipeline_step(project_id, step, "RUNNING", start, input_hash=input_hash)
            self.database.update_status(project_id, stage, start, f"{step} 단계를 시작합니다. ({attempt}/{retry['max_attempts']})")
            try:
                output = operation()
                self._check_cancel(cancel)
                break
            except PipelineCancelled:
                self.database.save_pipeline_step(project_id, step, "CANCELLED", start, input_hash=input_hash)
                raise
            except Exception as error:
                if cancel.is_set():
                    self.database.save_pipeline_step(project_id, step, "CANCELLED", start, input_hash=input_hash)
                    raise PipelineCancelled() from error
                self.database.save_pipeline_step(
                    project_id, step, "FAILED", start, error_message=str(error), input_hash=input_hash,
                )
                if attempt >= retry["max_attempts"]:
                    raise
                self.database.update_status(
                    project_id, stage, start,
                    f"{step} 실패 · {retry['backoff_sec']}초 후 {attempt + 1}번째 시도",
                )
                if cancel.wait(retry["backoff_sec"]):
                    self.database.save_pipeline_step(project_id, step, "CANCELLED", start, input_hash=input_hash)
                    raise PipelineCancelled() from error
        self.database.save_pipeline_step(project_id, step, "COMPLETE", end, output=output, input_hash=input_hash)
        self.database.update_status(project_id, stage, end, f"{step} 단계를 완료했습니다.")
        return output

    def _input_hash(self, source: str, options: dict[str, Any]) -> str:
        files = [source, options.get("manifest_path"), *(options.get("audio_paths") or []),
                 *(options.get("subtitle_paths") or {}).values()]
        payload = {
            "checkpoint_version": 2,
            "options": options,
            "files": [self._file_identity(value) for value in files if value],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        self._hash_context.value = digest
        return digest

    @staticmethod
    def _invoke(function, accepts_runner: bool, runner, *args, **kwargs):
        if accepts_runner:
            kwargs["runner"] = runner
        return function(*args, **kwargs)

    @staticmethod
    def _validate_retry_policy(raw: dict[str, Any]) -> dict[str, dict[str, float | int]]:
        if not isinstance(raw, dict):
            raise ValueError("retry_policy는 단계 이름을 키로 갖는 객체여야 합니다.")
        result = {}
        for step, value in raw.items():
            if not isinstance(value, dict):
                raise ValueError(f"{step} 재시도 정책은 객체여야 합니다.")
            attempts = int(value.get("max_attempts", 1))
            backoff = float(value.get("backoff_sec", 0))
            if attempts < 1 or backoff < 0:
                raise ValueError(f"{step} 재시도 횟수와 대기 시간은 음수가 될 수 없습니다.")
            result[str(step)] = {"max_attempts": attempts, "backoff_sec": backoff}
        return result

    def _calibrated_options(self, project: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        profile_id = project.get("calibration_profile_id")
        if not profile_id:
            return dict(options)
        profile = self.database.get_calibration(profile_id)
        defaults = profile["params"].get("pipeline_options", {})
        if not isinstance(defaults, dict):
            raise ValueError("캘리브레이션 pipeline_options는 객체여야 합니다.")
        return {**defaults, **options, "calibration_profile_version": profile["measured_at"]}

    @staticmethod
    def _analysis_chunks(duration_sec: float, chunk_sec: Any) -> list[dict[str, float]]:
        if chunk_sec is None:
            return [{"start_sec": 0.0, "end_sec": duration_sec}]
        size = float(chunk_sec)
        if size <= 0:
            raise ValueError("analysis_chunk_sec은 양수여야 합니다.")
        return [
            {"start_sec": start, "end_sec": min(duration_sec, start + size)}
            for start in (index * size for index in range(math.ceil(duration_sec / size)))
        ]

    @staticmethod
    def _chunk_progress(start: int, end: int, index: int, count: int) -> tuple[int, int]:
        return start + (end - start) * index // count, start + (end - start) * (index + 1) // count

    def _analyze_audio_chunk(self, runner, paths, duration, window, chunk, executable, output_path):
        if executable:
            return self._invoke(
                self.analyze_external_audio, self._default_external_audio,
                runner,
                executable, paths, output_path, duration, start_sec=chunk["start_sec"],
                end_sec=chunk["end_sec"], window_sec=window,
            )
        if self._default_audio:
            return self.analyze_audio(paths, duration, window_sec=window, ranges=[{**chunk, "reason": "chunk"}])
        return self.analyze_audio(paths, duration, window_sec=window)

    def _analyze_vision_chunk(self, runner, source, duration, interval, chunk, executable, output_path):
        if executable:
            return self._invoke(
                self.analyze_external_vision, self._default_external_vision, runner,
                executable, source, output_path, duration, start_sec=chunk["start_sec"],
                end_sec=chunk["end_sec"], interval_sec=interval,
            )
        if self._default_vision:
            return self.analyze_vision(
                source, duration, frame_interval_sec=interval, start_sec=chunk["start_sec"],
                end_sec=chunk["end_sec"], runner=runner,
            )
        return self.analyze_vision(source, duration, frame_interval_sec=interval)

    def _transcribe_chunk(self, runner, executable, paths, duration, output, language, chunk, ranged):
        if ranged:
            return self._invoke(
                self.transcribe_chunk, self._default_transcribe_chunk, runner,
                executable, paths, duration, output, start_sec=chunk["start_sec"],
                end_sec=chunk["end_sec"], language=language,
            )
        return self._invoke(
            self.transcribe, self._default_transcribe, runner,
            executable, paths, duration, output, language,
        )

    @staticmethod
    def _checkpoint_usable(step: str, checkpoint: dict[str, Any], input_hash: str) -> bool:
        if checkpoint.get("input_hash") != input_hash or checkpoint.get("corrupt_output"):
            return False
        output = checkpoint.get("output")
        if not isinstance(output, dict):
            return False
        if step == "PROBE":
            return isinstance(output.get("duration_sec"), (int, float)) and output["duration_sec"] > 0
        if step == "PREPROCESS":
            artifacts = output.get("artifacts")
            return isinstance(artifacts, list) and all(Path(item.get("path", "")).is_file() for item in artifacts)
        if step == "SCAN_PLAN":
            return isinstance(output.get("windows"), list)
        if step.startswith("STT"):
            return isinstance(output.get("segments"), list)
        if step.startswith("UNDERSTANDING_"):
            return isinstance(output.get("memory"), dict) and isinstance(output.get("precision_ranges"), list)
        if step.startswith(("AUDIO_ANALYSIS", "VISION_ANALYSIS", "PRECISION_ANALYSIS")):
            return isinstance(output.get("observations"), list)
        if step == "PRECISION_PLAN":
            return isinstance(output.get("ranges"), list)
        if step in {"AI_PRODUCER", "ANALYSIS_IMPORT"}:
            manifest = output.get("manifest", output)
            return isinstance(manifest, dict) and all(isinstance(manifest.get(key, []), list) for key in (
                "events", "candidates", "episodes",
            ))
        if step == "CONTENT_DISCOVERY":
            manifest = output.get("manifest")
            return isinstance(manifest, dict) and isinstance(manifest.get("events"), list) and isinstance(
                manifest.get("candidates"), list,
            )
        if step == "SCENE_RETRIEVAL":
            return isinstance(output.get("scenes"), list)
        if step == "DYNAMIC_PLANNING":
            return isinstance(output.get("manifest"), dict) and isinstance(output.get("episodes"), list)
        if step == "SMART_PACING":
            return isinstance(output.get("decisions"), list)
        if step == "PACKAGING_PLAN":
            return isinstance(output.get("packages"), list)
        if step.startswith("PACKAGE_"):
            paths = [output.get("json_path"), output.get("text_path"), *(output.get("thumbnails") or [])]
            return all(path and Path(path).is_file() for path in paths)
        if step.startswith("RENDER_"):
            return bool(output.get("output_path")) and Path(output["output_path"]).is_file()
        return True

    @staticmethod
    def _file_identity(value: str) -> dict[str, Any]:
        path = Path(value).expanduser().resolve()
        identity: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
        if not identity["exists"]:
            return identity
        stat = path.stat()
        checksum = hashlib.sha256()
        with path.open("rb") as source:
            checksum.update(source.read(1024 * 1024))
            if stat.st_size > 1024 * 1024:
                source.seek(max(0, stat.st_size - 1024 * 1024))
                checksum.update(source.read(1024 * 1024))
        identity.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns, sample_sha256=checksum.hexdigest())
        return identity

    @staticmethod
    def _check_cancel(event: threading.Event) -> None:
        if event.is_set():
            raise PipelineCancelled()

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
