from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .database import Database
from .media import probe_media
from .stt import transcribe_tracks
from .understanding import PreprocessPlan, build_scan_plan, execute_preprocess


class PipelineCancelled(RuntimeError):
    pass


class PipelineManager:
    """Checkpointed orchestration for the local, long-running analysis stages."""

    def __init__(
        self, database: Database, max_workers: int = 1, *,
        probe: Callable = probe_media, preprocess: Callable = execute_preprocess,
        transcribe: Callable = transcribe_tracks,
    ):
        self.database = database
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aicut")
        self.probe = probe
        self.preprocess = preprocess
        self.transcribe = transcribe
        self._jobs: dict[str, Future] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

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
        return True

    def state(self, project_id: str) -> dict:
        with self._lock:
            future = self._jobs.get(project_id)
            cancelling = bool(self._cancel.get(project_id) and self._cancel[project_id].is_set())
        return {
            "project_id": project_id, "running": bool(future and not future.done()),
            "done": bool(future and future.done()),
            "failed": bool(future and future.done() and future.exception()),
            "cancelling": cancelling, "steps": self.database.pipeline_steps(project_id),
        }

    def _run(self, project_id: str, options: dict[str, Any], resume: bool, cancel: threading.Event) -> None:
        try:
            completed = {item["step"]: item for item in self.database.pipeline_steps(project_id) if item["status"] == "COMPLETE"}
            project = self.database.get_project(project_id)
            media = self._step(project_id, "PROBE", "PARSING", 5, 15, cancel, resume, completed,
                               lambda: self.probe(project["file_path"]).to_dict())
            self.database.set_media_info(project_id, media)

            artifact_root = Path(options.get("output_directory") or Path("artifacts") / project_id).expanduser().resolve()
            if options.get("preprocess", False):
                result = self._step(
                    project_id, "PREPROCESS", "PARSING", 18, 35, cancel, resume, completed,
                    lambda: self.preprocess(PreprocessPlan(
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

            executable = options.get("stt_executable")
            if executable:
                audio_paths = options.get("audio_paths") or [
                    str(artifact_root / f"audio-track-{index:02d}.wav") for index in range(int(media["audio_tracks"]))
                ]
                stt = self._step(
                    project_id, "STT", "UNDERSTANDING", 45, 58, cancel, resume, completed,
                    lambda: self.transcribe(executable, audio_paths, float(media["duration_sec"]),
                                            artifact_root / "stt", options.get("language")),
                )
                self.database.replace_transcript(project_id, stt["segments"])

            manifest_path = options.get("manifest_path")
            if manifest_path:
                manifest = self._step(
                    project_id, "ANALYSIS_IMPORT", "DISCOVERING", 60, 75, cancel, resume, completed,
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

    def _step(self, project_id, step, stage, start, end, cancel, resume, completed, operation):
        self._check_cancel(cancel)
        if resume and step in completed:
            self.database.update_status(project_id, stage, end, f"{step} 체크포인트를 재사용합니다.")
            return completed[step]["output"]
        self.database.save_pipeline_step(project_id, step, "RUNNING", start)
        self.database.update_status(project_id, stage, start, f"{step} 단계를 시작합니다.")
        try:
            output = operation()
            self._check_cancel(cancel)
        except PipelineCancelled:
            self.database.save_pipeline_step(project_id, step, "CANCELLED", start)
            raise
        except Exception as error:
            self.database.save_pipeline_step(project_id, step, "FAILED", start, error_message=str(error))
            raise
        self.database.save_pipeline_step(project_id, step, "COMPLETE", end, output=output)
        self.database.update_status(project_id, stage, end, f"{step} 단계를 완료했습니다.")
        return output

    @staticmethod
    def _check_cancel(event: threading.Event) -> None:
        if event.is_set():
            raise PipelineCancelled()

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
