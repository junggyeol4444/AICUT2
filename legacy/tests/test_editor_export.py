import csv
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.editor_export import EditorExportError, export_editor_bundle


class EditorExportTest(unittest.TestCase):
    def test_exports_non_linear_active_cuts_to_fcpxml_edl_and_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "live show.mp4"
            source.write_bytes(b"video")
            result = export_editor_bundle(str(source), [
                {"cut_id": "ending", "source_start_sec": 50, "source_end_sec": 55,
                 "pacing_mode": "KEEP", "scene_role": "PAYOFF"},
                {"cut_id": "removed", "source_start_sec": 20, "source_end_sec": 30,
                 "pacing_mode": "CUT"},
                {"cut_id": "hook", "source_start_sec": 5, "source_end_sec": 8,
                 "pacing_mode": "TRIM", "pacing_reason": "fast hook"},
            ], Path(directory) / "exports", fps=30)
            clips = ET.parse(result["fcpxml"]).findall(".//asset-clip")
            with open(result["csv"], encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            edl = Path(result["edl"]).read_text()
        self.assertEqual([clip.attrib["name"] for clip in clips], ["ending", "hook"])
        self.assertEqual([row["cut_id"] for row in rows], ["ending", "hook"])
        self.assertIn("00:00:50:00 00:00:55:00", edl)
        self.assertEqual((result["active_cut_count"], result["duration_sec"]), (2, 8))

    def test_rejects_missing_source_invalid_fps_and_empty_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"video")
            with self.assertRaises(EditorExportError):
                export_editor_bundle("missing.mp4", [], directory)
            with self.assertRaises(EditorExportError):
                export_editor_bundle(str(source), [], directory)
            with self.assertRaises(EditorExportError):
                export_editor_bundle(str(source), [{
                    "source_start_sec": 0, "source_end_sec": 1, "pacing_mode": "KEEP",
                }], directory, fps=29)


if __name__ == "__main__":
    unittest.main()
