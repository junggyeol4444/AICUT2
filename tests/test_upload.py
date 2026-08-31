import json
import io
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from backend.database import Database
from backend.upload import (
    QuotaExceeded,
    UploadCancelled,
    UploadError,
    UploadManager,
    YouTubeResumableClient,
    next_quota_reset,
)


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def upload(self, file_path, metadata, privacy_status):
        self.calls.append((file_path, metadata, privacy_status))
        if self.error:
            raise self.error
        return "youtube-video-123"


class FakeResponse:
    def __init__(self, payload=b"", *, code=200, headers=None):
        self.payload, self.code, self.status = payload, code, code
        self.headers = headers or {}

    def read(self):
        return self.payload


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "upload.db")
        project = self.db.create_project({"file_path": "/media/live.mkv"})
        manifest = json.loads((Path(__file__).parent / "fixtures" / "analysis-manifest.json").read_text())
        self.db.import_analysis(project["project_id"], manifest)
        self.db.review_episode("episode-operation", True)
        self.db.set_render_status("episode-operation", "COMPLETE", "/output/episode.mp4")
        self.upload = self.db.queue_upload("episode-operation")

    def tearDown(self):
        self.temp.cleanup()

    def test_successful_upload_records_video_id(self):
        client = FakeClient()
        manager = UploadManager(self.db, client)
        self.assertTrue(manager.submit(self.upload["upload_id"]))
        manager.shutdown()
        job = self.db.list_uploads()[0]
        self.assertEqual((job["status"], job["youtube_video_id"]), ("COMPLETE", "youtube-video-123"))
        self.assertEqual(client.calls[0][2], "PRIVATE")
        published = self.db.record_upload_publication(self.upload["upload_id"], "PUBLIC")
        thumbnail = self.db.record_thumbnail_uploaded(self.upload["upload_id"])
        self.assertEqual(published["publication_status"], "PUBLIC")
        self.assertIsNotNone(thumbnail["thumbnail_uploaded_at"])

    def test_quota_retry_uses_next_pacific_midnight(self):
        # 2026-08-27 is PDT (UTC-7), so the next PT midnight is 07:00 UTC.
        reset = next_quota_reset(datetime(2026, 8, 27, 20, tzinfo=timezone.utc))
        self.assertEqual(reset, datetime(2026, 8, 28, 7, tzinfo=timezone.utc))
        client = FakeClient(QuotaExceeded("quotaExceeded"))
        manager = UploadManager(self.db, client)
        manager.submit(self.upload["upload_id"])
        manager.shutdown()
        job = self.db.list_uploads()[0]
        self.assertEqual(job["status"], "RETRY_QUEUED")
        self.assertIsNotNone(job["retry_at"])

    def test_winter_reset_observes_pacific_standard_time(self):
        reset = next_quota_reset(datetime(2026, 12, 15, 20, tzinfo=timezone.utc))
        self.assertEqual(reset, datetime(2026, 12, 16, 8, tzinfo=timezone.utc))

    def test_duplicate_submission_is_rejected_while_active(self):
        class BlockingClient(FakeClient):
            def upload(inner, *args):
                import time
                time.sleep(.05)
                return super(BlockingClient, inner).upload(*args)
        manager = UploadManager(self.db, BlockingClient())
        self.assertTrue(manager.submit(self.upload["upload_id"]))
        self.assertFalse(manager.submit(self.upload["upload_id"]))
        manager.shutdown()

    def test_resumable_client_uploads_chunks_and_reports_progress(self):
        calls, progress = [], []

        def opener(request):
            calls.append(request)
            if request.get_method() == "POST":
                return FakeResponse(headers={"Location": "https://upload.example/session"})
            if len(calls) == 2:
                return FakeResponse(code=308, headers={"Range": "bytes=0-262143"})
            return FakeResponse(json.dumps({"id": "video-real-1"}).encode())

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "episode.mp4"
            video.write_bytes(b"x" * 300_000)
            client = YouTubeResumableClient(
                "access-token", chunk_size=256 * 1024, opener=opener,
                progress=lambda sent, total: progress.append((sent, total)),
            )
            video_id = client.upload(str(video), {
                "title_options": ["선택 제목", "B", "C"], "description": "설명", "tags": ["게임"],
            }, "PRIVATE")
        self.assertEqual(video_id, "video-real-1")
        self.assertEqual([request.get_method() for request in calls], ["POST", "PUT", "PUT"])
        self.assertEqual(calls[1].get_header("Content-range"), "bytes 0-262143/300000")
        self.assertEqual(progress[-1], (300_000, 300_000))

    def test_resumable_client_stops_before_next_chunk_when_cancelled(self):
        calls = []
        cancelled = threading.Event()

        def opener(request):
            calls.append(request)
            if request.get_method() == "POST":
                return FakeResponse(headers={"Location": "https://upload.example/session"})
            return FakeResponse(code=308, headers={"Range": "bytes=0-262143"})

        def progress(_sent, _total):
            cancelled.set()

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "episode.mp4"
            video.write_bytes(b"x" * 300_000)
            client = YouTubeResumableClient(
                "access-token", chunk_size=256 * 1024, opener=opener, progress=progress,
            )
            with self.assertRaises(UploadCancelled):
                client.upload(
                    str(video), {"selected_title": "title"}, "PRIVATE",
                    cancel_event=cancelled,
                )
        self.assertEqual([request.get_method() for request in calls], ["POST", "PUT"])

    def test_resumable_client_continues_from_persisted_session_and_offset(self):
        calls = []

        def opener(request):
            calls.append(request)
            return FakeResponse(json.dumps({"id": "video-resumed"}).encode())

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "episode.mp4"
            video.write_bytes(b"x" * 300_000)
            client = YouTubeResumableClient("token", chunk_size=256 * 1024, opener=opener)
            video_id = client.upload(
                str(video), {"selected_title": "title"}, "PRIVATE",
                resume_session_url="https://upload.example/existing", resume_offset=262_144,
            )
        self.assertEqual(video_id, "video-resumed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get_method(), "PUT")
        self.assertEqual(calls[0].full_url, "https://upload.example/existing")
        self.assertEqual(calls[0].get_header("Content-range"), "bytes 262144-299999/300000")

    def test_manager_cancel_returns_job_to_retry_queue(self):
        class CancellableClient:
            def __init__(inner):
                inner.started = threading.Event()

            def upload(inner, _path, _metadata, _privacy, cancel_event=None):
                inner.started.set()
                self.assertIsNotNone(cancel_event)
                cancel_event.wait(1)
                raise UploadCancelled("cancelled")

        client = CancellableClient()
        manager = UploadManager(self.db, client)
        self.assertTrue(manager.submit(self.upload["upload_id"]))
        self.assertTrue(client.started.wait(1))
        self.assertTrue(manager.cancel(self.upload["upload_id"]))
        manager.shutdown()
        job = self.db.list_uploads()[0]
        self.assertEqual(job["status"], "RETRY_QUEUED")
        self.assertEqual(job["error_message"], "cancelled")
        self.assertFalse(manager.cancel(self.upload["upload_id"]))

    def test_manager_submits_only_uploads_whose_retry_deadline_is_due(self):
        future = self.db.queue_upload("episode-operation")
        self.db.set_upload_status(
            future["upload_id"], "RETRY_QUEUED",
            retry_at="2099-01-01T00:00:00+00:00", error_message="quota",
        )
        manager = UploadManager(self.db, FakeClient())
        result = manager.submit_due(datetime(2026, 8, 31, tzinfo=timezone.utc))
        manager.shutdown()
        statuses = {job["upload_id"]: job["status"] for job in self.db.list_uploads()}
        self.assertEqual(result, {
            "submitted": [self.upload["upload_id"]], "skipped": [future["upload_id"]],
        })
        self.assertEqual(statuses[self.upload["upload_id"]], "COMPLETE")
        self.assertEqual(statuses[future["upload_id"]], "RETRY_QUEUED")

    def test_manager_rejects_naive_retry_scheduler_time(self):
        manager = UploadManager(self.db, FakeClient())
        with self.assertRaises(ValueError):
            manager.submit_due(datetime(2026, 8, 31))
        manager.shutdown()

    def test_manager_persists_chunk_checkpoint_and_reuses_it_after_cancel(self):
        class CheckpointClient:
            def upload(
                inner, _path, _metadata, _privacy, cancel_event=None,
                resume_session_url=None, resume_offset=0, checkpoint=None,
            ):
                if not resume_session_url:
                    checkpoint("https://upload.example/durable", 262_144)
                    raise UploadCancelled("restart")
                self.assertEqual(resume_session_url, "https://upload.example/durable")
                self.assertEqual(resume_offset, 262_144)
                return "resumed-video"

        manager = UploadManager(self.db, CheckpointClient())
        manager.submit(self.upload["upload_id"])
        manager.shutdown()
        interrupted = self.db.list_uploads(include_resume_state=True)[0]
        self.assertEqual(interrupted["uploaded_bytes"], 262_144)
        self.assertEqual(interrupted["status"], "RETRY_QUEUED")
        self.assertNotIn("upload_session_url", self.db.list_uploads()[0])

        manager = UploadManager(self.db, CheckpointClient())
        manager.submit(self.upload["upload_id"])
        manager.shutdown()
        completed = self.db.list_uploads(include_resume_state=True)[0]
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertEqual(completed["uploaded_bytes"], 0)
        self.assertIsNone(completed["upload_session_url"])

    def test_resumable_client_maps_real_quota_reason(self):
        def opener(_request):
            raise HTTPError(
                "https://youtube.example", 403, "forbidden", {},
                io.BytesIO(b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'),
            )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "episode.mp4"
            video.write_bytes(b"video")
            client = YouTubeResumableClient("access-token", opener=opener)
            with self.assertRaises(QuotaExceeded):
                client.upload(str(video), {"selected_title": "title"}, "UNLISTED")

    def test_thumbnail_upload_uses_owned_video_id_and_image_content_type(self):
        calls = []

        def opener(request):
            calls.append(request)
            return FakeResponse(json.dumps({"items": [{"default": {"url": "thumbnail"}}]}).encode())

        with tempfile.TemporaryDirectory() as directory:
            thumbnail = Path(directory) / "thumbnail.jpg"
            thumbnail.write_bytes(b"jpeg")
            result = YouTubeResumableClient("token", opener=opener).upload_thumbnail("video-1", str(thumbnail))
        self.assertIn("videoId=video-1", calls[0].full_url)
        self.assertEqual(calls[0].get_header("Content-type"), "image/jpeg")
        self.assertIn("items", result)

    def test_publication_update_supports_public_and_scheduled_private(self):
        calls = []

        def opener(request):
            calls.append(json.loads(request.data))
            return FakeResponse(json.dumps({"id": "video-1", "status": calls[-1]["status"]}).encode())

        client = YouTubeResumableClient("token", opener=opener)
        client.update_video_status("video-1", "PUBLIC")
        client.update_video_status(
            "video-1", "PRIVATE", datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(calls[0]["status"]["privacyStatus"], "public")
        self.assertEqual(calls[1]["status"]["publishAt"], "2099-01-01T00:00:00Z")
        with self.assertRaises(UploadError):
            client.update_video_status("video-1", "PUBLIC", datetime(2099, 1, 1, tzinfo=timezone.utc))
