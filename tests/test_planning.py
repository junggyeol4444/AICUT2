import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.planning import PlanningError, run_planning, validate_edit_plan


class PlanningTests(unittest.TestCase):
    def episode(self):
        return {"episodes": [{
            "episode_id": "ep-1", "candidate_ids": ["candidate-1"],
            "target_type": "LONG", "structure": {"strategy": "결과를 먼저 암시"},
            "timeline": [
                {"source_start_sec": 90, "source_end_sec": 100, "scene_role": "예고",
                 "pacing_mode": "TRIM", "selection_reason": "결과를 암시",
                 "pacing_reason": "반응 직전의 마만 보존"},
                {"source_start_sec": 10, "source_end_sec": 30, "scene_role": "배경",
                 "pacing_mode": "KEEP", "selection_reason": "사건의 발단",
                 "pacing_reason": "맥락 전달에 필요"},
            ],
        }]}

    def test_validates_non_linear_plan_and_computes_duration(self):
        result = validate_edit_plan(self.episode(), {"candidate-1"}, 120)
        self.assertEqual(result["episodes"][0]["computed_active_duration_sec"], 30)
        self.assertEqual(result["episodes"][0]["timeline"][1]["source_start_sec"], 10)

    def test_requires_explainable_pacing(self):
        payload = self.episode()
        del payload["episodes"][0]["timeline"][0]["pacing_reason"]
        with self.assertRaisesRegex(PlanningError, "호흡 판단 근거"):
            validate_edit_plan(payload, {"candidate-1"}, 120)

    def test_rejects_unknown_candidate(self):
        with self.assertRaisesRegex(PlanningError, "후보 참조"):
            validate_edit_plan(self.episode(), {"candidate-other"}, 120)

    def test_runner_only_sends_make_or_combine_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            def runner(command, **_kwargs):
                request = json.loads(Path(command[-3]).read_text(encoding="utf-8"))
                self.assertEqual([item["candidate_id"] for item in request["candidates"]], ["candidate-1"])
                self.assertTrue(request["instructions"]["target_duration_is_hint_only"])
                Path(command[-1]).write_text(json.dumps(self.episode()), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            result = run_planning(
                ["producer"],
                [{"candidate_id": "candidate-1", "decision": "MAKE"},
                 {"candidate_id": "candidate-2", "decision": "REJECT"}],
                [], [], 120, directory, "8~12분", runner,
            )
            self.assertEqual(result["plan"]["episodes"][0]["episode_id"], "ep-1")


if __name__ == "__main__":
    unittest.main()
