import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.longterm import LongTermUnderstandingError, run_window_understanding


class LongTermUnderstandingTest(unittest.TestCase):
    def test_window_model_receives_memory_and_returns_precision_hints(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "window.json"

            def runner(command, **_kwargs):
                input_path = Path(command[command.index("--input") + 1])
                request = json.loads(input_path.read_text(encoding="utf-8"))
                self.assertEqual(request["memory"]["topic"], "challenge")
                output.write_text(json.dumps({
                    "summary": "도전 결과를 기다리는 중",
                    "memory": {"topic": "challenge", "state": "pending"},
                    "precision_ranges": [{"start_sec": 15, "end_sec": 18, "reason": "uncertain_result"}],
                }), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = run_window_understanding(
                ["understanding-model"], {"start_sec": 0, "end_sec": 20}, [], {"topic": "challenge"},
                output, runner=runner,
            )
            self.assertEqual(result["memory"]["state"], "pending")
            self.assertEqual(result["precision_ranges"][0]["reason"], "uncertain_result")

    def test_precision_hint_must_stay_inside_current_window(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "window.json"

            def runner(_command, **_kwargs):
                output.write_text(json.dumps({"summary": "summary", "memory": {},
                                              "precision_ranges": [{"start_sec": 19, "end_sec": 21}]}))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(LongTermUnderstandingError):
                run_window_understanding(
                    ["model"], {"start_sec": 0, "end_sec": 20}, [], {}, output, runner=runner,
                )


if __name__ == "__main__":
    unittest.main()
