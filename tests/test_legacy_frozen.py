"""`legacy/` stays out of the suite, and stays intact.

The Codex implementation is kept as a record: `docs/merge.ko.md` cites it by
line number, and the four operational modules ported into `aicut/` were taken
from it. It is not maintained, and it does not run.

That is fine while it is invisible to test discovery. It stops being fine the
moment someone adds an `__init__.py`, because `legacy/backend/subtitles.py`
does not parse below Python 3.12 — one f-string with a backslash in the
expression, which PEP 701 only allowed in 3.12. Discovery would then take down
the whole suite with a SyntaxError from a tree nobody meant to run, and the
message would point at a file the reader has no reason to be looking at.

So this says it out loud, first, with the reason attached.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy"


class LegacyFrozenTests(unittest.TestCase):
    def test_the_tree_is_still_there(self):
        """Deleting it would orphan every line-number citation in merge.ko.md."""
        self.assertTrue((LEGACY / "backend").is_dir())
        self.assertTrue((LEGACY / "src").is_dir())
        self.assertTrue((LEGACY / "README.md").is_file())

    def test_it_is_not_collectable_by_test_discovery(self):
        collectable = [
            str(path.relative_to(ROOT))
            for path in (LEGACY, LEGACY / "tests")
            if (path / "__init__.py").is_file()
        ]
        self.assertEqual(collectable, [], (
            "legacy/ has become importable as a package, so `unittest discover` will "
            "now try to import it. It is frozen, unmaintained Codex code and "
            "legacy/backend/subtitles.py raises SyntaxError below Python 3.12 — the "
            "whole suite will fail from a tree nobody meant to run. Remove the "
            "__init__.py, or read legacy/README.md and decide deliberately."
        ))

    def test_the_ported_modules_still_have_their_source_to_compare_against(self):
        """Each entry in merge.ko.md section 3 needs both halves to stay checkable."""
        for original, ported in (
            ("backend/auth.py", "aicut/ui/auth.py"),
            ("backend/token_store.py", "aicut/intelligence/token_store.py"),
            ("backend/backup.py", "aicut/db/backup.py"),
            ("backend/scheduler.py", "aicut/scheduler.py"),
        ):
            with self.subTest(original=original):
                self.assertTrue((LEGACY / original).is_file(), f"{original} is gone")
                self.assertTrue((ROOT / ported).is_file(), f"{ported} is gone")


if __name__ == "__main__":
    unittest.main()
