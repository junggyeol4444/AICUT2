from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from .database import Database


class UploadError(RuntimeError):
    pass


class QuotaExceeded(UploadError):
    pass


class UploadClient(Protocol):
    def upload(self, file_path: str, metadata: dict, privacy_status: str) -> str: ...


def next_quota_reset(now: datetime | None = None) -> datetime:
    """Return the next YouTube quota reset at Pacific Time midnight, in UTC."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now는 timezone-aware datetime이어야 합니다.")
    pacific = current.astimezone(ZoneInfo("America/Los_Angeles"))
    tomorrow = pacific.date() + timedelta(days=1)
    reset = datetime.combine(tomorrow, datetime.min.time(), ZoneInfo("America/Los_Angeles"))
    return reset.astimezone(timezone.utc)


class UploadManager:
    def __init__(self, database: Database, client: UploadClient, max_workers: int = 1):
        self.database = database
        self.client = client
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aicut-upload")
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, upload_id: str) -> bool:
        with self._lock:
            if upload_id in self._active:
                return False
            self._active.add(upload_id)
        self.executor.submit(self._run, upload_id)
        return True

    def _run(self, upload_id: str) -> None:
        try:
            jobs = [job for job in self.database.list_uploads() if job["upload_id"] == upload_id]
            if not jobs:
                raise KeyError(upload_id)
            job = jobs[0]
            if job["status"] not in {"QUEUED", "RETRY_QUEUED"}:
                raise UploadError(f"현재 상태에서는 업로드할 수 없습니다: {job['status']}")
            self.database.set_upload_status(upload_id, "UPLOADING")
            video_id = self.client.upload(job["output_mp4_path"], job["metadata"], job["privacy_status"])
            self.database.set_upload_status(upload_id, "COMPLETE", youtube_video_id=video_id)
        except QuotaExceeded as error:
            self.database.set_upload_status(
                upload_id, "RETRY_QUEUED", retry_at=next_quota_reset().isoformat(), error_message=str(error),
            )
        except Exception as error:
            try:
                self.database.set_upload_status(upload_id, "FAILED", error_message=str(error))
            except KeyError:
                pass
        finally:
            with self._lock:
                self._active.discard(upload_id)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)


class UnconfiguredYouTubeClient:
    def upload(self, file_path: str, metadata: dict, privacy_status: str) -> str:
        raise UploadError("YouTube OAuth 클라이언트가 설정되지 않았습니다.")
