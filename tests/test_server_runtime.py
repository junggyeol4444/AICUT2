import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend.server import create_runtime


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


if __name__ == "__main__":
    unittest.main()
