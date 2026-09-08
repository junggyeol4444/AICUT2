import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend.intelligence import IntelligenceError, run_reference_analyzer, validate_reference_analysis


def sample():
    return {
        "video_id": "video-1", "title": "reference", "channel_ref": "channel",
        "duration_sec": 300, "public_metrics": {"views": 1000, "likes": 40},
        "patterns": {
            "structure": ["result_first"], "editing": {"cut_rhythm": "variable"},
            "storytelling": ["delayed_context"], "scene_selection": ["reaction"],
            "pacing": {"silence": "contextual"}, "subtitles": ["reaction_emphasis"],
            "title_thumbnail_relationship": "question_and_result", "production_logic": "결과를 먼저 보여준다",
        },
    }


class IntelligenceTest(unittest.TestCase):
    def test_validates_and_persists_derived_reference_knowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "db.sqlite3")
            first = database.save_youtube_reference(validate_reference_analysis(sample()))
            changed = sample(); changed["public_metrics"]["views"] = 2000
            second = database.save_youtube_reference(validate_reference_analysis(changed))
            rows = database.list_youtube_references()
        self.assertEqual(first["reference_id"], second["reference_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["public_metrics"]["views"], 2000)

    def test_rejects_private_metrics_for_reference_channels(self):
        value = sample(); value["public_metrics"]["audience_retention"] = [1, .5]
        with self.assertRaises(IntelligenceError):
            validate_reference_analysis(value)

    def test_external_contract_writes_and_validates_result(self):
        with tempfile.TemporaryDirectory() as directory:
            def runner(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text(json.dumps(sample()), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            result = run_reference_analyzer(["model"], {"video_id": "video-1"}, directory, runner)
        self.assertEqual(result["analysis"]["video_id"], "video-1")


if __name__ == "__main__":
    unittest.main()
