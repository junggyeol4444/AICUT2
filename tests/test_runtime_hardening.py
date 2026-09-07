import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.database import Database
from backend.server import ApiHandler


class RuntimeHardeningTest(unittest.TestCase):
    def test_importing_server_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "import-side-effect.sqlite3"
            environment = {**os.environ, "AICUT_DB": str(target)}
            subprocess.run(
                [sys.executable, "-c", "import backend.server"], cwd=Path(__file__).parents[1],
                env=environment, check=True,
            )
            self.assertFalse(target.exists())

    def test_database_uses_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "runtime.sqlite3", timeout=7)
            with database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 7000)

    def test_missing_frontend_never_falls_back_to_repository_root(self):
        class Response:
            value = None
            def json(self, value, status):
                self.value = (value, status)
        response = Response()
        with tempfile.TemporaryDirectory() as directory, patch("backend.server.ROOT", Path(directory)):
            ApiHandler.serve_static(response, "/backend/token_store.py")
        self.assertEqual(response.value[0]["error"], "frontend_not_built")
        self.assertEqual(int(response.value[1]), 503)

    def test_repository_files_are_not_spa_fallbacks(self):
        class Response:
            value = None
            def json(self, value, status):
                self.value = (value, status)
        response = Response()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            (root / "dist" / "index.html").write_text("public app")
            (root / "aicut.db").write_bytes(b"private database")
            with patch("backend.server.ROOT", root):
                ApiHandler.serve_static(response, "/aicut.db")
        self.assertEqual(response.value[0]["error"], "not_found")
        self.assertEqual(int(response.value[1]), 404)


if __name__ == "__main__":
    unittest.main()
