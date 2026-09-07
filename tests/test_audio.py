import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.audio import AudioAnalyzerError, run_audio_analyzer


class AudioAnalyzerTest(unittest.TestCase):
    def test_external_audio_events_keep_track_and_source_time(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            mic, call = Path(directory) / "mic.wav", Path(directory) / "call.wav"
            mic.touch()
            call.touch()

            def runner(command, **_kwargs):
                output.write_text(json.dumps({"observations": [{
                    "kind": "LAUGHTER", "track_index": 1, "start_sec": 12, "end_sec": 13,
                    "confidence": .95, "payload": {"embedding": [0.1, 0.2]},
                }]}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = run_audio_analyzer(
                ["audio-model"], [mic, call], output, 100,
                start_sec=10, end_sec=20, window_sec=.5, runner=runner,
            )
            event = result["observations"][0]
            self.assertEqual((event["modality"], event["track_index"], event["kind"]),
                             ("AUDIO", 1, "LAUGHTER"))
            self.assertEqual(result["command"].count("--audio"), 2)

    def test_external_audio_event_rejects_unknown_track(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            mic = Path(directory) / "mic.wav"
            mic.touch()

            def runner(_command, **_kwargs):
                output.write_text(json.dumps({"observations": [{
                    "kind": "SCREAM", "track_index": 3, "start_sec": 10, "end_sec": 11,
                    "payload": {},
                }]}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(AudioAnalyzerError):
                run_audio_analyzer(
                    ["audio-model"], [mic], output, 100,
                    start_sec=10, end_sec=20, window_sec=1, runner=runner,
                )


if __name__ == "__main__":
    unittest.main()
