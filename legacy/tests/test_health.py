import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.database import Database
from backend.health import runtime_readiness


class RuntimeReadinessTest(unittest.TestCase):
    def test_ready_when_database_storage_scheduler_and_tools_are_available(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "aicut.db")
            result = runtime_readiness(
                database, Path(directory) / "outputs", {"running": True, "last_run_at": "now"},
                min_free_bytes=1, required_tools=("ffmpeg",), which=lambda _tool: "/bin/tool",
            )
        self.assertEqual(result["status"], "READY")
        self.assertTrue(all(item["ok"] for item in result["checks"].values()))

    def test_reports_missing_tools_and_low_disk_without_hiding_other_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "aicut.db")
            result = runtime_readiness(
                database, directory, {"running": False}, min_free_bytes=100,
                required_tools=("ffmpeg", "model-runner"),
                which=lambda tool: "/bin/ffmpeg" if tool == "ffmpeg" else None,
                disk_usage=lambda _path: SimpleNamespace(free=50),
            )
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["checks"]["tools"]["missing"], ["model-runner"])
        self.assertFalse(result["checks"]["storage"]["ok"])
        self.assertFalse(result["checks"]["scheduler"]["ok"])
        self.assertTrue(result["checks"]["database"]["ok"])

    def test_rejects_negative_disk_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "aicut.db")
            with self.assertRaises(ValueError):
                runtime_readiness(database, directory, {"running": True}, min_free_bytes=-1)


if __name__ == "__main__":
    unittest.main()
