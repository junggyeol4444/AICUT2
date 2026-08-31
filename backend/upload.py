from __future__ import annotations

import threading
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .database import Database


class UploadError(RuntimeError):
    pass


class QuotaExceeded(UploadError):
    pass


class UploadClient(Protocol):
    def upload(self, file_path: str, metadata: dict, privacy_status: str) -> str: ...


class YouTubeResumableClient:
    """YouTube Data API resumable uploader using an already refreshed OAuth access token."""

    endpoint = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"

    def __init__(
        self, access_token: str, *, chunk_size: int = 8 * 1024 * 1024,
        opener: Callable = urlopen, progress: Callable[[int, int], None] | None = None,
    ):
        if not access_token.strip():
            raise UploadError("YouTube OAuth access token이 필요합니다.")
        if chunk_size <= 0 or chunk_size % (256 * 1024):
            raise UploadError("resumable upload chunk_size는 256KiB의 배수여야 합니다.")
        self.access_token = access_token
        self.chunk_size = chunk_size
        self.opener = opener
        self.progress = progress or (lambda _sent, _total: None)

    def upload(self, file_path: str, metadata: dict, privacy_status: str) -> str:
        path = os.path.abspath(os.path.expanduser(file_path))
        if not os.path.isfile(path):
            raise UploadError(f"업로드할 영상 파일을 찾을 수 없습니다: {path}")
        if privacy_status not in {"PRIVATE", "UNLISTED"}:
            raise UploadError("검수 게이트 업로드는 PRIVATE 또는 UNLISTED만 허용됩니다.")
        titles = metadata.get("title_options") or []
        title = str(metadata.get("selected_title") or (titles[0] if titles else "")).strip()
        if not title:
            raise UploadError("업로드 메타데이터에 선택된 제목이 필요합니다.")
        total = os.path.getsize(path)
        if total <= 0:
            raise UploadError("빈 영상 파일은 업로드할 수 없습니다.")
        body = json.dumps({
            "snippet": {"title": title, "description": str(metadata.get("description", "")),
                        "tags": list(metadata.get("tags", []))},
            "status": {"privacyStatus": privacy_status.lower()},
        }, ensure_ascii=False).encode()
        request = Request(self.endpoint, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json; charset=UTF-8",
            "Content-Length": str(len(body)), "X-Upload-Content-Length": str(total),
            "X-Upload-Content-Type": "video/mp4",
        })
        response = self._open(request)
        session_url = response.headers.get("Location")
        if not session_url:
            raise UploadError("YouTube가 resumable upload 세션 URL을 반환하지 않았습니다.")
        sent = 0
        with open(path, "rb") as source:
            while sent < total:
                chunk = source.read(min(self.chunk_size, total - sent))
                end = sent + len(chunk) - 1
                upload_request = Request(session_url, data=chunk, method="PUT", headers={
                    "Authorization": f"Bearer {self.access_token}", "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)), "Content-Range": f"bytes {sent}-{end}/{total}",
                })
                response = self._open(upload_request, allow_resume=True)
                if getattr(response, "code", getattr(response, "status", None)) == 308:
                    accepted = response.headers.get("Range", "").rsplit("-", 1)[-1]
                    sent = int(accepted) + 1 if accepted.isdigit() else end + 1
                    source.seek(sent)
                else:
                    sent = end + 1
                self.progress(sent, total)
        try:
            payload = json.loads(response.read().decode("utf-8"))
            video_id = str(payload["id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise UploadError("YouTube 업로드 완료 응답에 video id가 없습니다.") from error
        return video_id

    def _open(self, request: Request, allow_resume: bool = False):
        try:
            return self.opener(request)
        except HTTPError as error:
            payload = error.read().decode("utf-8", errors="replace")
            if allow_resume and error.code == 308:
                return error
            if error.code == 403 and any(reason in payload for reason in ("quotaExceeded", "dailyLimitExceeded")):
                raise QuotaExceeded("YouTube Data API 업로드 쿼터를 초과했습니다.") from error
            raise UploadError(f"YouTube API 오류 ({error.code}): {payload[-2000:]}") from error
        except URLError as error:
            raise UploadError(f"YouTube 업로드 네트워크 오류: {error.reason}") from error


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
            self.database.schedule_analytics_snapshots(job["episode_id"], video_id)
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


def client_from_environment() -> UploadClient:
    token = os.environ.get("YOUTUBE_ACCESS_TOKEN", "").strip()
    return YouTubeResumableClient(token) if token else UnconfiguredYouTubeClient()
