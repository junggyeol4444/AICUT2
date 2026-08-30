from __future__ import annotations

import json
import hashlib
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .database import Database
from .media import check_disk_capacity, probe_media
from .multimodal import analyze_audio_tracks, analyze_precision_ranges, analyze_video
from .producer import run_producer
from .processes import ProcessSupervisor
from .stt import transcribe_tracks
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
        analyze_audio: Callable = analyze_audio_tracks,
        analyze_vision: Callable = analyze_video,
        analyze_precision: Callable = analyze_precision_ranges,
        analyze_external_vision: Callable = run_vision_analyzer,
        produce: Callable = run_producer,
    ):
        self.database = database
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aicut")
        self.probe = probe
        self.check_disk = check_disk
        self.preprocess = preprocess
        self.transcribe = transcribe
        self.analyze_audio = analyze_audio
        self.analyze_vision = analyze_vision
        self.analyze_precision = analyze_precision
        self.analyze_external_vision = analyze_external_vision
        self.produce = produce
        self._default_preprocess = preprocess is execute_preprocess
        self._default_transcribe = transcribe is transcribe_tracks
        self._default_audio = analyze_audio is analyze_audio_tracks
        self._default_vision = analyze_vision is analyze_video
        self._default_precision = analyze_precision is analyze_precision_ranges
        self._default_external_vision = analyze_external_vision is run_vision_analyzer
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
                chunks = self._analysis_chunks(
                    float(media["duration_sec"]), options.get("analysis_chunk_sec") if self._default_audio else None,
                )
                for index, chunk in enumerate(chunks):
                    step = "AUDIO_ANALYSIS" if len(chunks) == 1 else f"AUDIO_ANALYSIS_{index:06d}"
                    progress = self._chunk_progress(43, 50, index, len(chunks))
                    audio = self._step(
                        project_id, step, "UNDERSTANDING", progress[0], progress[1], cancel, resume, completed,
                        lambda chunk=chunk: self._analyze_audio_chunk(
                            audio_paths, float(media["duration_sec"]), float(options.get("audio_window_sec", 1.0)),
                            chunk,
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
                stt = self._step(
                    project_id, "STT", "UNDERSTANDING", 45, 58, cancel, resume, completed,
                    lambda: self._invoke(self.transcribe, self._default_transcribe, runner,
                                            executable, audio_paths, float(media["duration_sec"]),
                                            artifact_root / "stt", options.get("language")),
                )
                self.database.replace_transcript(project_id, stt["segments"])

            precision_policy = options.get("precision_policy")
            if precision_policy:
                analysis = self.database.analysis_input(project_id)
                precision = self._step(
                    project_id, "PRECISION_PLAN", "UNDERSTANDING", 59, 60, cancel, resume, completed,
                    lambda: {"ranges": select_precision_ranges(
                        float(media["duration_sec"]), analysis["transcript"], analysis["observations"], precision_policy,
                    )},
                )
                combined_ranges = list(options.get("precision_ranges") or []) + precision["ranges"]
                self.database.replace_scan_windows(project_id, [window.__dict__ for window in build_scan_plan(
                    float(media["duration_sec"]), float(options.get("coarse_window_sec", 300)), combined_ranges,
                )])
                if options.get("precision_analysis") and precision["ranges"]:
                    detailed = self._step(
                        project_id, "PRECISION_ANALYSIS", "UNDERSTANDING", 60, 68, cancel, resume, completed,
                        lambda: self._invoke(self.analyze_precision, self._default_precision, runner,
                            project["file_path"], [path for path in audio_paths if Path(path).is_file()],
                            float(media["duration_sec"]), precision["ranges"],
                            audio_window_sec=float(options["precision_audio_window_sec"]),
                            vision_interval_sec=float(options["precision_vision_interval_sec"]),
                        ),
                    )
                    self.database.replace_precision_observations(project_id, detailed["observations"])

            producer_executable = options.get("producer_executable")
            manifest_path = options.get("manifest_path")
            if producer_executable:
                produced = self._step(
                    project_id, "AI_PRODUCER", "DISCOVERING", 70, 80, cancel, resume, completed,
                    lambda: self._invoke(self.produce, self._default_producer, runner,
                                         producer_executable, self.database.analysis_input(project_id),
                                         artifact_root / "producer", float(media["duration_sec"])),
                )
                self.database.import_analysis(project_id, produced["manifest"])
            elif manifest_path:
                manifest = self._step(
                    project_id, "ANALYSIS_IMPORT", "DISCOVERING", 70, 80, cancel, resume, completed,
                    lambda: json.loads(Path(manifest_path).expanduser().resolve().read_text(encoding="utf-8")),
                )
                self.database.import_analysis(project_id, manifest)
            else:
                self.database.update_status(
                    project_id, "UNDERSTANDING", 58 if executable else 42,
                    "전처리 파이프라인 완료 · 장기 방송 이해 AI 결과를 기다립니다.",
                )
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
        files = [source, options.get("manifest_path"), *(options.get("audio_paths") or [])]
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

    def _analyze_audio_chunk(self, paths, duration, window, chunk):
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
        if step == "STT":
            return isinstance(output.get("segments"), list)
        if step.startswith(("AUDIO_ANALYSIS", "VISION_ANALYSIS", "PRECISION_ANALYSIS")):
            return isinstance(output.get("observations"), list)
        if step == "PRECISION_PLAN":
            return isinstance(output.get("ranges"), list)
        if step in {"AI_PRODUCER", "ANALYSIS_IMPORT"}:
            manifest = output.get("manifest", output)
            return isinstance(manifest, dict) and all(isinstance(manifest.get(key, []), list) for key in (
                "events", "candidates", "episodes",
            ))
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
