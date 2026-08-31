import json
import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend.performance import (
    PerformanceError, attribute_retention_to_cuts, performance_insights, validate_metrics,
)


class PerformanceLearningTest(unittest.TestCase):
    def setUp(self):
        self.metrics = {
            "views": 12000, "likes": 840, "comments": 120, "shares": 44,
            "click_through_rate": .082, "average_view_percentage": .61,
            "retention": [
                {"second": 0, "ratio": 1.0}, {"second": 30, "ratio": .66},
                {"second": 90, "ratio": .60}, {"second": 120, "ratio": .72},
            ],
        }
        self.profile = {"early_window_sec": 30, "early_drop_ratio": .25, "peak_gain_ratio": .1}

    def test_detects_early_drop_and_retention_gain_from_external_profile(self):
        insights = performance_insights(self.metrics, self.profile)
        self.assertEqual([item["kind"] for item in insights], ["EARLY_DROP", "RETENTION_GAIN"])
        self.assertEqual([item["at_sec"] for item in insights], [30, 120])

    def test_requires_channel_measured_thresholds(self):
        with self.assertRaises(PerformanceError):
            performance_insights(self.metrics, {})

    def test_rejects_invalid_publication_metrics(self):
        with self.assertRaises(PerformanceError):
            validate_metrics({**self.metrics, "views": -1})
        invalid_retention = {**self.metrics, "retention": [{"second": 10, "ratio": 1.2}]}
        with self.assertRaises(PerformanceError):
            validate_metrics(invalid_retention)

    def test_snapshot_is_persisted_for_owned_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "performance.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            manifest = json.loads((Path(__file__).parent / "fixtures" / "analysis-manifest.json").read_text())
            database.import_analysis(project["project_id"], manifest)
            saved = database.save_performance("episode-operation", validate_metrics(self.metrics))
            snapshots = database.list_performance("episode-operation")
        self.assertEqual(snapshots[0]["performance_id"], saved["performance_id"])
        self.assertEqual(snapshots[0]["metrics"]["views"], 12000)

    def test_retention_changes_are_mapped_to_non_linear_cut_roles(self):
        cuts = [
            {"sequence_order": 1, "source_start_sec": 500, "source_end_sec": 560,
             "scene_role": "RESULT", "pacing_mode": "KEEP"},
            {"sequence_order": 2, "source_start_sec": 100, "source_end_sec": 200,
             "scene_role": "CONTEXT", "pacing_mode": "TRIM"},
        ]
        result = attribute_retention_to_cuts(self.metrics, cuts, {
            "min_views": 1000, "min_retention_points": 4, "meaningful_change_ratio": .1,
        })
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["observations"][0]["scene_role"], "RESULT")
        self.assertEqual(result["observations"][0]["source_start_sec"], 500)
        self.assertTrue(all(item["interpretation"] == "CORRELATION_ONLY" for item in result["observations"]))

    def test_cut_attribution_marks_small_samples_instead_of_learning(self):
        result = attribute_retention_to_cuts({**self.metrics, "views": 5}, [], {
            "min_views": 100, "min_retention_points": 4, "meaningful_change_ratio": .1,
        })
        self.assertEqual(result["status"], "INSUFFICIENT_SAMPLE")
        self.assertIn("MINIMUM_VIEWS_NOT_MET", result["reasons"])


if __name__ == "__main__":
    unittest.main()
