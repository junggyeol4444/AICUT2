from __future__ import annotations

import hashlib
import inspect
import json
import mimetypes
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .database import Database


class UploadError(RuntimeError):
    pass


class QuotaExceeded(UploadError):
    pass


class UploadCancelled(UploadError):
    pass


class TransientUploadError(UploadError):
    pass


class ExpiredUploadSession(TransientUploadError):
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

    def upload(
        self, file_path: str, metadata: dict, privacy_status: str,
        cancel_event: threading.Event | None = None,
        resume_session_url: str | None = None, resume_offset: int = 0,
        checkpoint: Callable[[str, int], None] | None = None,
    ) -> str:
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
        if resume_offset < 0 or resume_offset >= total or (resume_offset and not resume_session_url):
            raise UploadError("저장된 resumable upload 진행 위치가 영상 범위를 벗어났습니다.")
        body = json.dumps({
            "snippet": {"title": title, "description": str(metadata.get("description", "")),
                        "tags": list(metadata.get("tags", []))},
            "status": {"privacyStatus": privacy_status.lower()},
        }, ensure_ascii=False).encode()
        session_url = resume_session_url
        if not session_url:
            request = Request(self.endpoint, data=body, method="POST", headers={
                "Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json; charset=UTF-8",
                "Content-Length": str(len(body)), "X-Upload-Content-Length": str(total),
                "X-Upload-Content-Type": "video/mp4",
            })
            response = self._open(request)
            session_url = response.headers.get("Location")
            if not session_url:
                raise UploadError("YouTube가 resumable upload 세션 URL을 반환하지 않았습니다.")
            if checkpoint:
                checkpoint(session_url, 0)
        sent = resume_offset
        with open(path, "rb") as source:
            source.seek(sent)
            while sent < total:
                if cancel_event and cancel_event.is_set():
                    raise UploadCancelled("사용자 요청으로 YouTube 업로드를 취소했습니다.")
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
                    if checkpoint:
                        checkpoint(session_url, sent)
                else:
                    sent = end + 1
                self.progress(sent, total)
        try:
            payload = json.loads(response.read().decode("utf-8"))
            video_id = str(payload["id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise UploadError("YouTube 업로드 완료 응답에 video id가 없습니다.") from error
        return video_id

    def upload_thumbnail(self, video_id: str, image_path: str) -> dict:
        path = os.path.abspath(os.path.expanduser(image_path))
        content_type = mimetypes.guess_type(path)[0]
        if not video_id or not os.path.isfile(path) or content_type not in {"image/jpeg", "image/png"}:
            raise UploadError("썸네일에는 업로드된 video id와 JPEG 또는 PNG 파일이 필요합니다.")
        with open(path, "rb") as source:
            data = source.read()
        endpoint = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?{urlencode({'videoId': video_id})}"
        response = self._open(Request(endpoint, data=data, method="POST", headers={
            "Authorization": f"Bearer {self.access_token}", "Content-Type": content_type,
            "Content-Length": str(len(data)),
        }))
        return self._json_response(response, "YouTube 썸네일 응답이 올바르지 않습니다.")

    def update_video_status(
        self, video_id: str, privacy_status: str, publish_at: datetime | None = None,
    ) -> dict:
        if privacy_status not in {"PRIVATE", "UNLISTED", "PUBLIC"} or not video_id:
            raise UploadError("영상 공개 상태 또는 video id가 올바르지 않습니다.")
        status = {"privacyStatus": privacy_status.lower()}
        if publish_at:
            if publish_at.tzinfo is None or publish_at <= datetime.now(timezone.utc):
                raise UploadError("예약 공개 시각은 timezone-aware 미래 시각이어야 합니다.")
            if privacy_status != "PRIVATE":
                raise UploadError("YouTube 예약 공개는 PRIVATE 상태에서만 설정할 수 있습니다.")
            status["publishAt"] = publish_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        data = json.dumps({"id": video_id, "status": status}).encode()
        response = self._open(Request(
            "https://www.googleapis.com/youtube/v3/videos?part=status", data=data, method="PUT", headers={
                "Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json",
                "Content-Length": str(len(data)),
            },
        ))
        return self._json_response(response, "YouTube 공개 상태 응답이 올바르지 않습니다.")

    @staticmethod
    def _json_response(response, message: str) -> dict:
        try:
            payload = json.loads(response.read().decode("utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise UploadError(message) from error
        if not isinstance(payload, dict):
            raise UploadError(message)
        return payload

    def _open(self, request: Request, allow_resume: bool = False):
        try:
            return self.opener(request)
        except HTTPError as error:
            payload = error.read().decode("utf-8", errors="replace")
            if allow_resume and error.code == 308:
                return error
            if allow_resume and error.code in {404, 410}:
                raise ExpiredUploadSession("YouTube resumable upload 세션이 만료되었습니다.") from error
            if error.code == 403 and any(reason in payload for reason in ("quotaExceeded", "dailyLimitExceeded")):
                raise QuotaExceeded("YouTube Data API 업로드 쿼터를 초과했습니다.") from error
            if error.code in {408, 429, 500, 502, 503, 504}:
                raise TransientUploadError(f"YouTube API 임시 오류 ({error.code})") from error
            raise UploadError(f"YouTube API 오류 ({error.code}): {payload[-2000:]}") from error
        except URLError as error:
            raise TransientUploadError(f"YouTube 업로드 네트워크 오류: {error.reason}") from error


def next_quota_reset(now: datetime | None = None) -> datetime:
    """Return the next YouTube quota reset at Pacific Time midnight, in UTC."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now는 timezone-aware datetime이어야 합니다.")
    pacific = current.astimezone(ZoneInfo("America/Los_Angeles"))
    tomorrow = pacific.date() + timedelta(days=1)
    reset = datetime.combine(tomorrow, datetime.min.time(), ZoneInfo("America/Los_Angeles"))
    return reset.astimezone(timezone.utc)


def transient_retry_at(
    upload_id: str, attempt_count: int, now: datetime | None = None,
    *, base_delay_sec: float = 30, max_delay_sec: float = 3600,
) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or attempt_count <= 0 or base_delay_sec <= 0 or max_delay_sec <= 0:
        raise ValueError("transient retry 계산 인자가 올바르지 않습니다.")
    exponential = base_delay_sec * (2 ** min(attempt_count - 1, 20))
    fraction = int(hashlib.sha256(upload_id.encode()).hexdigest()[:4], 16) / 65535
    jittered = exponential * (.75 + .5 * fraction)
    return current + timedelta(seconds=min(jittered, max_delay_sec))


class UploadManager:
    def __init__(
        self, database: Database, client: UploadClient, max_workers: int = 1,
        *, recover_interrupted: bool = True, transient_base_delay_sec: float = 30,
        transient_max_delay_sec: float = 3600,
    ):
        self.database = database
        self.client = client
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aicut-upload")
        self._active: set[str] = set()
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        if transient_base_delay_sec <= 0 or transient_max_delay_sec <= 0:
            raise ValueError("upload transient retry delay는 0보다 커야 합니다.")
        self.transient_base_delay_sec = transient_base_delay_sec
        self.transient_max_delay_sec = transient_max_delay_sec
        self.recovered_upload_ids = (
            self.database.recover_interrupted_uploads() if recover_interrupted else []
        )

    def submit(self, upload_id: str) -> bool:
        with self._lock:
            if upload_id in self._active:
                return False
            self._active.add(upload_id)
            self._cancel[upload_id] = threading.Event()
        self.executor.submit(self._run, upload_id)
        return True

    def cancel(self, upload_id: str) -> bool:
        with self._lock:
            event = self._cancel.get(upload_id)
            active = upload_id in self._active
        if not event or not active:
            return False
        event.set()
        return True

    def submit_due(self, moment: datetime | None = None) -> dict[str, list[str]]:
        """Submit queued jobs and retry jobs whose UTC deadline has elapsed."""
        current = moment or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("업로드 재시도 기준 시각에는 timezone 정보가 필요합니다.")
        current = current.astimezone(timezone.utc)
        submitted: list[str] = []
        skipped: list[str] = []
        for job in reversed(self.database.list_uploads(include_resume_state=True)):
            if job["status"] not in {"QUEUED", "RETRY_QUEUED"}:
                continue
            retry_at = job.get("retry_at")
            if retry_at:
                deadline = datetime.fromisoformat(retry_at)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if deadline.astimezone(timezone.utc) > current:
                    skipped.append(job["upload_id"])
                    continue
            if self.submit(job["upload_id"]):
                submitted.append(job["upload_id"])
            else:
                skipped.append(job["upload_id"])
        return {"submitted": submitted, "skipped": skipped}

    def _run(self, upload_id: str) -> None:
        try:
            jobs = [
                job for job in self.database.list_uploads(include_resume_state=True)
                if job["upload_id"] == upload_id
            ]
            if not jobs:
                raise KeyError(upload_id)
            job = jobs[0]
            if job["status"] not in {"QUEUED", "RETRY_QUEUED"}:
                raise UploadError(f"현재 상태에서는 업로드할 수 없습니다: {job['status']}")
            started = self.database.set_upload_status(upload_id, "UPLOADING")
            event = self._cancel[upload_id]
            parameters = inspect.signature(self.client.upload).parameters
            kwargs = {"cancel_event": event} if "cancel_event" in parameters else {}
            if "resume_session_url" in parameters:
                kwargs.update({
                    "resume_session_url": job.get("upload_session_url"),
                    "resume_offset": int(job.get("uploaded_bytes") or 0),
                    "checkpoint": lambda url, offset: self.database.set_upload_progress(
                        upload_id, url, offset,
                    ),
                })
            video_id = self.client.upload(
                job["output_mp4_path"], job["metadata"], job["privacy_status"], **kwargs,
            )
            self.database.set_upload_progress(upload_id, None, 0)
            self.database.set_upload_status(upload_id, "COMPLETE", youtube_video_id=video_id)
            self.database.schedule_analytics_snapshots(job["episode_id"], video_id)
        except UploadCancelled as error:
            self.database.set_upload_status(upload_id, "RETRY_QUEUED", error_message=str(error))
        except QuotaExceeded as error:
            self.database.set_upload_status(
                upload_id, "RETRY_QUEUED", retry_at=next_quota_reset().isoformat(), error_message=str(error),
            )
        except ExpiredUploadSession as error:
            self.database.set_upload_progress(upload_id, None, 0)
            retry_at = transient_retry_at(
                upload_id, int(started["attempt_count"]),
                base_delay_sec=self.transient_base_delay_sec,
                max_delay_sec=self.transient_max_delay_sec,
            )
            self.database.set_upload_status(
                upload_id, "RETRY_QUEUED", retry_at=retry_at.isoformat(), error_message=str(error),
            )
        except TransientUploadError as error:
            retry_at = transient_retry_at(
                upload_id, int(started["attempt_count"]),
                base_delay_sec=self.transient_base_delay_sec,
                max_delay_sec=self.transient_max_delay_sec,
            )
            self.database.set_upload_status(
                upload_id, "RETRY_QUEUED", retry_at=retry_at.isoformat(), error_message=str(error),
            )
        except Exception as error:
            try:
                self.database.set_upload_status(upload_id, "FAILED", error_message=str(error))
            except KeyError:
                pass
        finally:
            with self._lock:
                self._active.discard(upload_id)
                self._cancel.pop(upload_id, None)

    def cancel_all(self) -> int:
        with self._lock:
            events = list(self._cancel.values())
        for event in events:
            event.set()
        return len(events)

    def shutdown(self, *, cancel_running: bool = True) -> None:
        if cancel_running:
            self.cancel_all()
        self.executor.shutdown(wait=True, cancel_futures=True)


class UnconfiguredYouTubeClient:
    def upload(self, file_path: str, metadata: dict, privacy_status: str) -> str:
        raise UploadError("YouTube OAuth 클라이언트가 설정되지 않았습니다.")


def client_from_environment() -> UploadClient:
    token = os.environ.get("YOUTUBE_ACCESS_TOKEN", "").strip()
    return YouTubeResumableClient(token) if token else UnconfiguredYouTubeClient()
