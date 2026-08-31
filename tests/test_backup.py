import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.backup import DatabaseBackupManager
from backend.database import Database


class DatabaseBackupTest(unittest.TestCase):
    def test_atomic_backup_contains_committed_database_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "aicut.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            result = DatabaseBackupManager(database, root / "backups").create()
            with sqlite3.connect(result["path"]) as connection:
                stored = connection.execute(
                    "SELECT project_id FROM projects WHERE project_id=?", (project["project_id"],),
                ).fetchone()
        self.assertEqual(stored[0], project["project_id"])
        self.assertGreater(result["size_bytes"], 0)

    def test_retention_prunes_only_old_aicut_backups(self):
        moments = iter([
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 2, tzinfo=timezone.utc),
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "aicut.db")
            backup_dir = root / "backups"
            backup_dir.mkdir()
            unrelated = backup_dir / "manual.sqlite3"
            unrelated.write_bytes(b"keep")
            manager = DatabaseBackupManager(
                database, backup_dir, retention_count=1, clock=lambda: next(moments),
            )
            first = manager.create()
            second = manager.create()
            owned = list(backup_dir.glob("aicut-*.sqlite3"))
            unrelated_exists = unrelated.exists()
        self.assertEqual(len(owned), 1)
        self.assertIn(first["path"], second["removed"])
        self.assertTrue(unrelated_exists)

    def test_invalid_retention_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "aicut.db")
            with self.assertRaises(ValueError):
                DatabaseBackupManager(database, directory, retention_count=0)


if __name__ == "__main__":
    unittest.main()
