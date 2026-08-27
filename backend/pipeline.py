from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from .database import Database
from .media import probe_media


class PipelineManager:
    """Runs bounded local media jobs while keeping every transition durable."""

    def __init__(self, database: Database, max_workers: int = 1):
        self.database = database
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aicut")
        self._jobs: dict[str, Future] = {}
        self._lock = threading.Lock()

    def submit(self, project_id: str, manifest_path: str | None = None) -> bool:
        with self._lock:
            running = self._jobs.get(project_id)
            if running and not running.done():
                return False
            self._jobs[project_id] = self.executor.submit(self._run, project_id, manifest_path)
            return True

    def state(self, project_id: str) -> dict:
        with self._lock:
            future = self._jobs.get(project_id)
        return {
            "project_id": project_id,
            "running": bool(future and not future.done()),
            "done": bool(future and future.done()),
            "failed": bool(future and future.done() and future.exception()),
        }

    def _run(self, project_id: str, manifest_path: str | None) -> None:
        project = self.database.get_project(project_id)
        try:
            self.database.update_status(project_id, "PARSING", 5, "ffprobe 미디어 검사를 시작합니다.")
            info = probe_media(project["file_path"])
            self.database.set_media_info(project_id, info.to_dict())
            self.database.update_status(
                project_id, "PARSING", 15,
                f"미디어 검사 완료 · {info.width}x{info.height}, 오디오 {info.audio_tracks}트랙",
            )
            if manifest_path:
                path = Path(manifest_path).expanduser().resolve()
                manifest = json.loads(path.read_text(encoding="utf-8"))
                self.database.update_status(project_id, "UNDERSTANDING", 55, "외부 멀티모달 분석 결과를 검증합니다.")
                self.database.import_analysis(project_id, manifest)
            else:
                self.database.update_status(
                    project_id, "UNDERSTANDING", 20,
                    "미디어 파싱 완료 · 멀티모달 분석 매니페스트 입력을 기다립니다.",
                )
        except Exception as error:
            self.database.fail_project(project_id, str(error))

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
