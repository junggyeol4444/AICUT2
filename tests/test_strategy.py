import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend.strategy import aggregate_edit_strategies


def snapshot(identifier, change):
    return {"performance_id": identifier, "metrics": {"cut_attribution": {
        "status": "READY", "observations": [{
            "scene_role": "RESULT", "pacing_mode": "KEEP", "retention_change": change,
        }],
    }}}


class StrategyLearningTest(unittest.TestCase):
    def setUp(self):
        self.profile = {"min_snapshots": 3, "min_observations": 3, "confidence_z": 1,
                        "minimum_effect": .1}

    def test_promotes_only_when_confidence_interval_clears_effect_threshold(self):
        result = aggregate_edit_strategies([
            snapshot("one", .18), snapshot("two", .20), snapshot("three", .22),
        ], self.profile)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["decision"], "PROMOTE")
        self.assertGreaterEqual(proposal["interval"]["lower"], .1)
        self.assertEqual(proposal["interpretation"], "CORRELATION_ONLY_REQUIRES_EXPERIMENT")

    def test_holds_strategy_when_cross_video_sample_is_insufficient(self):
        result = aggregate_edit_strategies([snapshot("one", .5), snapshot("two", .5)], self.profile)
        self.assertEqual(result["proposals"][0]["decision"], "HOLD")
        self.assertEqual(result["proposals"][0]["reason"], "INSUFFICIENT_SAMPLE")

    def test_strategy_versions_require_explicit_activation_and_rollback_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "strategy.db")
            first = database.save_strategy_version("channel", {"proposals": [{"decision": "PROMOTE"}]})
            second = database.save_strategy_version("channel", {"proposals": [{"decision": "HOLD"}]})
            database.activate_strategy_version(first["strategy_version_id"])
            database.activate_strategy_version(second["strategy_version_id"])
            versions = database.list_strategy_versions("channel")
        self.assertEqual(versions[0]["status"], "ACTIVE")
        self.assertEqual(versions[1]["status"], "ROLLED_BACK")
        self.assertEqual([item["version_number"] for item in versions], [2, 1])


if __name__ == "__main__":
    unittest.main()
