import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend.learning import MappingError, Segment, analyze_source_output, complement, merge_intervals


class SourceOutputLearningTest(unittest.TestCase):
    def test_discovers_selection_removal_reordering_repetition_and_emphasis(self):
        cuts = [
            {"source_start_sec": 60, "source_end_sec": 80, "visual_effect": {"type": "zoom"}},
            {"source_start_sec": 10, "source_end_sec": 20, "visual_effect": {}},
            {"source_start_sec": 65, "source_end_sec": 72, "visual_effect": {"type": "replay"}},
        ]
        result = analyze_source_output(100, cuts)
        self.assertEqual(result["output_duration_sec"], 37)
        self.assertEqual(result["selected_segments"], [
            {"start_sec": 10, "end_sec": 20}, {"start_sec": 60, "end_sec": 80},
        ])
        self.assertEqual(len(result["reordered_cuts"]), 1)
        self.assertEqual(result["repeated_sources"][0]["overlap_sec"], 7)
        self.assertEqual([item["effect"]["type"] for item in result["emphasized_cuts"]], ["zoom", "replay"])
        self.assertAlmostEqual(result["selection_ratio"], .3)

    def test_uses_sequence_order_and_excludes_cut_decisions_from_training_output(self):
        result = analyze_source_output(100, [
            {"sequence_order": 3, "source_start_sec": 80, "source_end_sec": 90,
             "scene_role": "discarded", "pacing_mode": "CUT"},
            {"sequence_order": 2, "source_start_sec": 10, "source_end_sec": 20,
             "scene_role": "context", "pacing_mode": "TRIM"},
            {"sequence_order": 1, "source_start_sec": 50, "source_end_sec": 60,
             "scene_role": "result", "pacing_mode": "KEEP", "speaker_tag": "HOST"},
        ])
        self.assertEqual(result["output_duration_sec"], 20)
        self.assertEqual(result["selection_ratio"], .2)
        self.assertEqual(result["removed_segments"], [
            {"start_sec": 0, "end_sec": 10}, {"start_sec": 20, "end_sec": 50},
            {"start_sec": 60, "end_sec": 100},
        ])
        self.assertEqual(result["scene_role_summary"], {"result": 1, "context": 1})
        self.assertEqual(result["pacing_summary"], {"KEEP": 1, "TRIM": 1})
        self.assertTrue(result["cut_decisions"][1]["is_reordered"])
        self.assertNotIn("discarded", result["scene_role_summary"])
        self.assertEqual(result["discarded_cuts"][0]["scene_role"], "discarded")

    def test_all_cut_output_preserves_explicit_editor_rejections(self):
        result = analyze_source_output(100, [{
            "sequence_order": 1, "source_start_sec": 30, "source_end_sec": 40,
            "scene_role": "repetition", "pacing_mode": "CUT", "pacing_reason": "중복 장면",
        }])
        self.assertEqual(result["output_duration_sec"], 0)
        self.assertEqual(result["discarded_cuts"][0]["reason"], "중복 장면")

    def test_rejects_duplicate_sequence_order(self):
        with self.assertRaisesRegex(MappingError, "중복"):
            analyze_source_output(100, [
                {"sequence_order": 1, "source_start_sec": 10, "source_end_sec": 20},
                {"sequence_order": 1, "source_start_sec": 30, "source_end_sec": 40},
            ])

    def test_interval_merge_and_complement_preserve_source_coverage(self):
        selected = merge_intervals([Segment(20, 30), Segment(5, 10), Segment(8, 22)])
        self.assertEqual(selected, [Segment(5, 30)])
        self.assertEqual(complement(selected, 40), [Segment(0, 5), Segment(30, 40)])

    def test_empty_output_is_valid_and_means_everything_was_removed(self):
        result = analyze_source_output(3600, [])
        self.assertEqual(result["selection_ratio"], 0)
        self.assertEqual(result["removed_segments"], [{"start_sec": 0, "end_sec": 3600}])

    def test_invalid_ranges_are_rejected(self):
        with self.assertRaises(MappingError):
            analyze_source_output(0, [])
        with self.assertRaises(MappingError):
            analyze_source_output(100, [{"source_start_sec": 90, "source_end_sec": 110}])

    def test_analysis_is_saved_as_training_pair(self):
        analysis = analyze_source_output(100, [{"source_start_sec": 10, "source_end_sec": 20}])
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "learning.db")
            project = database.create_project({"file_path": "/media/source.mkv"})
            saved = database.save_source_output_pair(
                "/media/source.mkv", "/output/final.mp4", analysis, project["project_id"],
            )
            pairs = database.list_source_output_pairs(project["project_id"])
        self.assertEqual(pairs[0]["pair_id"], saved["pair_id"])
        self.assertEqual(pairs[0]["selection_analysis"]["selection_ratio"], .1)
