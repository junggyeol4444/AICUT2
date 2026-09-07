import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.pacing import PacingError, run_smart_pacing


def input_package():
    return {"episodes": [{"episode_id": "episode-1", "timeline": [
        {"sequence_order": 1, "source_start_sec": 10, "source_end_sec": 12},
        {"sequence_order": 2, "source_start_sec": 20, "source_end_sec": 25},
    ]}]}


class SmartPacingTest(unittest.TestCase):
    def test_pacing_keeps_contextual_silence_and_cuts_idle_work(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def runner(_command, **_kwargs):
                (output / "pacing-output.json").write_text(json.dumps({"decisions": [
                    {"episode_id": "episode-1", "sequence_order": 1, "pacing_mode": "KEEP",
                     "reason": "reaction pause"},
                    {"episode_id": "episode-1", "sequence_order": 2, "pacing_mode": "CUT",
                     "reason": "repetitive idle work"},
                ]}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = run_smart_pacing(["pacing-model"], input_package(), output, runner)
            self.assertEqual([item["pacing_mode"] for item in result["decisions"]], ["KEEP", "CUT"])

    def test_pacing_rejects_unknown_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def runner(_command, **_kwargs):
                (output / "pacing-output.json").write_text(json.dumps({"decisions": [{
                    "episode_id": "episode-1", "sequence_order": 99, "pacing_mode": "CUT", "reason": "idle",
                }]}))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(PacingError):
                run_smart_pacing(["model"], input_package(), output, runner)


if __name__ == "__main__":
    unittest.main()
