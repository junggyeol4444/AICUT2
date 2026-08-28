import json
import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend.performance import PerformanceError, performance_insights, validate_metrics


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


if __name__ == "__main__":
    unittest.main()
