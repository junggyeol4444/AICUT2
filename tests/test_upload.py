import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.database import Database
from backend.upload import QuotaExceeded, UploadManager, next_quota_reset


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def upload(self, file_path, metadata, privacy_status):
        self.calls.append((file_path, metadata, privacy_status))
        if self.error:
            raise self.error
        return "youtube-video-123"


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
