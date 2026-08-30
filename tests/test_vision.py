import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.vision import VisionAnalyzerError, run_vision_analyzer


class VisionAnalyzerTest(unittest.TestCase):
    def test_external_observations_are_validated_on_the_requested_source_range(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"

            def runner(command, **_kwargs):
                output.write_text(json.dumps({"observations": [{
                    "kind": "FACE_APPEARED", "start_sec": 10, "end_sec": 11,
                    "confidence": .9, "payload": {"person_id": "person-1"},
                }]}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = run_vision_analyzer(
                ["vision-model"], "/media/live.mkv", output, 100,
                start_sec=10, end_sec=20, interval_sec=.5, runner=runner,
            )
            self.assertEqual(result["observations"][0]["modality"], "VISION")
            self.assertIn("--start-sec", result["command"])

    def test_external_observations_cannot_escape_the_requested_chunk(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"

            def runner(_command, **_kwargs):
                output.write_text(json.dumps({"observations": [{
                    "kind": "OCR_TEXT", "start_sec": 9, "end_sec": 11, "payload": {},
                }]}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(VisionAnalyzerError):
                run_vision_analyzer(
                    ["vision-model"], "/media/live.mkv", output, 100,
                    start_sec=10, end_sec=20, interval_sec=1, runner=runner,
                )


if __name__ == "__main__":
    unittest.main()
