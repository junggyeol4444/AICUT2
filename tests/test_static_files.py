import tempfile
import unittest
from pathlib import Path

from backend.static_files import StaticFileError, resolve_static_file


class StaticFilesTest(unittest.TestCase):
    def test_only_serves_files_from_the_distribution_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            distribution = root / "dist"
            distribution.mkdir()
            (distribution / "index.html").write_text("public", encoding="utf-8")
            (root / "aicut.db").write_text("private", encoding="utf-8")

            self.assertEqual(resolve_static_file(distribution, "/"), distribution / "index.html")
            with self.assertRaises(StaticFileError):
                resolve_static_file(distribution, "/../aicut.db")
            with self.assertRaises(FileNotFoundError):
                resolve_static_file(distribution, "/aicut.db")
            with self.assertRaises(FileNotFoundError):
                resolve_static_file(distribution, "/backend/token_store.py")

    def test_missing_distribution_never_falls_back_to_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "aicut.db").write_text("private", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                resolve_static_file(root / "dist", "/aicut.db")


if __name__ == "__main__":
    unittest.main()
