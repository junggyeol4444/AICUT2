import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.planning import PlanningError, run_dynamic_planner


def analysis_input():
    return {
        "events": [{"event_id": "event-1", "summary": "challenge", "mentions": []}],
        "candidates": [{"candidate_id": "candidate-1", "summary": "challenge", "event_ids": ["event-1"],
                        "independence_score": .9, "decision": "MAKE", "decision_reason": "complete"}],
        "retrieved_scenes": [{"candidate_id": "candidate-1", "start_sec": 10, "end_sec": 20}],
    }


class DynamicPlanningTest(unittest.TestCase):
    def test_planner_can_create_non_chronological_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def runner(_command, **_kwargs):
                (output / "planning-output.json").write_text(json.dumps({"episodes": [{
                    "episode_id": "episode-1", "candidate_ids": ["candidate-1"], "target_type": "LONG",
                    "timeline": [
                        {"source_start_sec": 50, "source_end_sec": 55, "scene_role": "result", "pacing_mode": "KEEP"},
                        {"source_start_sec": 10, "source_end_sec": 20, "scene_role": "context", "pacing_mode": "TRIM"},
                    ],
                }]}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = run_dynamic_planner(["planner"], analysis_input(), output, 100, runner)
            timeline = result["episodes"][0]["timeline"]
            self.assertEqual([item["source_start_sec"] for item in timeline], [50, 10])

    def test_planner_cannot_reference_an_unknown_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def runner(_command, **_kwargs):
                (output / "planning-output.json").write_text(json.dumps({"episodes": [{
                    "episode_id": "episode-1", "candidate_ids": ["missing"], "target_type": "LONG",
                    "timeline": [],
                }]}))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(PlanningError):
                run_dynamic_planner(["planner"], analysis_input(), output, 100, runner)


if __name__ == "__main__":
    unittest.main()
