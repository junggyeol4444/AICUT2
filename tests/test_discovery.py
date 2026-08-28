import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.discovery import (
    DiscoveryError, build_discovery_command, run_discovery, validate_discovery_manifest,
)


class ContentDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((Path(__file__).parent / "fixtures" / "analysis-manifest.json").read_text())

    def test_command_uses_argument_array(self):
        command = build_discovery_command(["python3", "producer.py"], "input.json", "output.json")
        self.assertEqual(command, ["python3", "producer.py", "--input", "input.json", "--output", "output.json"])

    def test_validates_event_candidate_and_non_linear_timeline_references(self):
        result = validate_discovery_manifest(self.manifest, 4000)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["candidates"][0]["event_ids"], ["event-operation"])
        starts = [cut["source_start_sec"] for cut in result["episodes"][0]["timeline"]]
        self.assertNotEqual(starts, sorted(starts))

    def test_zero_content_is_a_valid_result(self):
        result = validate_discovery_manifest({"events": [], "candidates": [], "episodes": []}, 100)
        self.assertEqual(result, {"events": [], "candidates": [], "episodes": []})

    def test_rejects_dangling_event_and_unexplained_decision(self):
        invalid = json.loads(json.dumps(self.manifest))
        invalid["candidates"][0]["event_ids"] = ["missing"]
        with self.assertRaises(DiscoveryError):
            validate_discovery_manifest(invalid, 4000)
        invalid = json.loads(json.dumps(self.manifest))
        invalid["candidates"][0]["decision_reason"] = ""
        with self.assertRaises(DiscoveryError):
            validate_discovery_manifest(invalid, 4000)

    def test_external_producer_receives_event_not_screen_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            def runner(command, **kwargs):
                payload = json.loads((work / "understanding-input.json").read_text())
                self.assertEqual(payload["instructions"]["boundary"], "event_not_screen_state")
                self.assertTrue(payload["instructions"]["allow_zero_candidates"])
                (work / "discovery-output.json").write_text(json.dumps(self.manifest))
                return SimpleNamespace(returncode=0, stderr="")
            result = run_discovery(["producer"], [{"start_sec": 0, "end_sec": 10}], 4000, work, runner)
        self.assertEqual(result["manifest"]["candidates"][0]["candidate_id"], "candidate-operation")

    def test_missing_output_is_an_error(self):
        runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DiscoveryError):
            run_discovery(["producer"], [], 100, directory, runner)


if __name__ == "__main__":
    unittest.main()
