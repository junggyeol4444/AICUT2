import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.discovery import DiscoveryError, run_content_discovery


class ContentDiscoveryTest(unittest.TestCase):
    def test_event_candidates_are_validated_without_edit_episodes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def runner(_command, **_kwargs):
                (output / "discovery-output.json").write_text(json.dumps({
                    "events": [{"event_id": "event-1", "summary": "challenge",
                                "mentions": [{"start_sec": 1, "end_sec": 2, "role": "origin"}]}],
                    "candidates": [{"candidate_id": "candidate-1", "summary": "complete challenge",
                                    "event_ids": ["event-1"], "independence_score": .9,
                                    "decision": "MAKE", "decision_reason": "complete event"}],
                }), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = run_content_discovery(["model"], {"timeline": []}, output, 10, runner=runner)
            self.assertEqual(result["manifest"]["candidates"][0]["decision"], "MAKE")
            self.assertEqual(result["manifest"]["episodes"], [])

    def test_discovery_cannot_bypass_planning_by_returning_episodes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            def runner(*_args, **_kwargs):
                (output / "discovery-output.json").write_text(json.dumps({"episodes": [{}]}))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            with self.assertRaises(DiscoveryError):
                run_content_discovery(["model"], {}, output, 10, runner=runner)


if __name__ == "__main__":
    unittest.main()
