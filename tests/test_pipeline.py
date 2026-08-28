import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.database import Database
from backend.pipeline import PipelineManager


class PipelineTest(unittest.TestCase):
    def test_pipeline_persists_steps_and_reuses_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            calls = []

            def probe(_path):
                calls.append("probe")
                return SimpleNamespace(to_dict=lambda: {
                    "duration_sec": 100, "width": 1920, "height": 1080, "audio_tracks": 0,
                })

            manager = PipelineManager(database, probe=probe)
            manager._run(project["project_id"], {"coarse_window_sec": 30}, True, threading.Event())
            self.assertEqual(
                [step["step"] for step in database.pipeline_steps(project["project_id"])],
                ["PROBE", "SCAN_PLAN"],
            )
            self.assertEqual(database.get_project(project["project_id"])["status"], "UNDERSTANDING")
            manager._run(project["project_id"], {"coarse_window_sec": 30}, True, threading.Event())
            self.assertEqual(calls, ["probe"])
            manager.shutdown()

    def test_failed_step_is_durable_and_project_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "pipeline.db")
            project = database.create_project({"file_path": "/missing.mkv"})
            manager = PipelineManager(database, probe=lambda _path: (_ for _ in ()).throw(RuntimeError("probe failed")))
            manager._run(project["project_id"], {}, True, threading.Event())
            self.assertEqual(database.get_project(project["project_id"])["status"], "FAILED")
            self.assertEqual(database.pipeline_steps(project["project_id"])[0]["status"], "FAILED")
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
