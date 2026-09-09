import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from backend.server import create_runtime, create_server


class ServerRuntimeTest(unittest.TestCase):
    def test_importing_server_has_no_database_or_configuration_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "must-not-exist.db"
            environment = {
                **os.environ,
                "AICUT_DB": str(database),
                "AICUT_MAX_REQUEST_BYTES": "0",
            }
            result = subprocess.run(
                [sys.executable, "-c", "import backend.server"],
                cwd=Path(__file__).resolve().parents[1], env=environment,
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(database.exists())

    def test_runtime_factory_validates_configuration_before_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "must-not-exist.db"
            with self.assertRaisesRegex(ValueError, "0보다 커야"):
                create_runtime({"AICUT_DB": str(database), "AICUT_MAX_REQUEST_BYTES": "0"})
            self.assertFalse(database.exists())

    def test_runtime_factory_uses_injected_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.db"
            runtime = create_runtime({
                "AICUT_DB": str(database),
                "AICUT_BACKUP_DIR": str(Path(directory) / "backups"),
                "AICUT_API_KEY": "runtime-secret",
                "YOUTUBE_ACCESS_TOKEN": "youtube-secret",
            })
            try:
                self.assertEqual(runtime.database.path, str(database))
                self.assertTrue(runtime.auth.enabled)
                self.assertEqual(runtime.max_request_bytes, 1024 * 1024)
                self.assertEqual(runtime.uploads.client.access_token, "youtube-secret")
            finally:
                runtime.shutdown()

    def test_source_output_learning_can_use_a_rendered_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = create_runtime({
                "AICUT_DB": str(Path(directory) / "runtime.db"),
                "AICUT_BACKUP_DIR": str(Path(directory) / "backups"),
            })
            project = runtime.database.create_project({"file_path": "/media/live.mkv", "duration_sec": 100})
            manifest = {
                "events": [], "candidates": [],
                "episodes": [{
                    "episode_id": "episode-one", "candidate_ids": [], "target_type": "LONG",
                    "timeline": [{
                        "source_start_sec": 10, "source_end_sec": 20, "scene_role": "context",
                        "pacing_mode": "KEEP",
                    }],
                }],
            }
            runtime.database.import_analysis(project["project_id"], manifest)
            runtime.database.set_render_status("episode-one", "COMPLETE", "/output/final.mp4")
            server = create_server("127.0.0.1", 0, runtime)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/learning/source-output",
                    data=json.dumps({"episode_id": "episode-one"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    saved = json.load(response)
                self.assertEqual(saved["project_id"], project["project_id"])
                self.assertEqual(saved["source_ref"], "/media/live.mkv")
                self.assertEqual(saved["output_ref"], "/output/final.mp4")
                self.assertEqual(saved["selection_analysis"]["selection_ratio"], .1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
