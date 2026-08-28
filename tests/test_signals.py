import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from backend.signals import (
    SignalError, assemble_analysis_windows, extract_pcm_features, validate_visual_observations,
)


class MultimodalSignalTest(unittest.TestCase):
    def _wave(self, path: Path, seconds=1, rate=8000, frequency=440):
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            samples = [int(12000 * math.sin(2 * math.pi * frequency * index / rate)) for index in range(rate * seconds)]
            stream.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))

    def test_extracts_pcm_features_in_configured_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "track.wav"
            self._wave(source, seconds=2)
            features = extract_pcm_features(source, track_index=2, window_sec=.5)
        self.assertEqual(len(features), 4)
        self.assertTrue(all(item["track_index"] == 2 for item in features))
        self.assertTrue(all(item["rms_dbfs"] < 0 for item in features))
        self.assertTrue(all(0 <= item["zero_crossing_rate"] <= 1 for item in features))

    def test_rejects_missing_audio_and_unmeasured_window(self):
        with self.assertRaises(SignalError):
            extract_pcm_features("/missing.wav", 0, 1)
        with self.assertRaises(SignalError):
            extract_pcm_features("anything.wav", 0, 0)

    def test_visual_observations_are_validated_and_sorted(self):
        values = validate_visual_observations([
            {"second": 8, "situation": "게임", "motion_score": .8},
            {"second": 2, "situation": "토크", "people": ["JUNE"]},
        ], 10)
        self.assertEqual([item["second"] for item in values], [2, 8])
        with self.assertRaises(SignalError):
            validate_visual_observations([{"second": 11}], 10)

    def test_assembles_timestamp_aligned_multimodal_windows(self):
        scan = [
            {"pass_kind": "COARSE", "start_sec": 0, "end_sec": 10, "reason": "full"},
            {"pass_kind": "COARSE", "start_sec": 10, "end_sec": 20, "reason": "full"},
        ]
        transcript = [{"start_sec": 8, "end_sec": 12, "text": "경계 발화"}]
        audio = [{"start_sec": 10, "end_sec": 11, "rms_dbfs": -10}]
        visual = [{"second": 15, "screen_event": "승리"}]
        windows = assemble_analysis_windows(scan, transcript, audio, visual)
        self.assertEqual(len(windows[0]["transcript"]), 1)
        self.assertEqual(len(windows[1]["transcript"]), 1)
        self.assertEqual(windows[1]["audio"][0]["rms_dbfs"], -10)
        self.assertEqual(windows[1]["visual"][0]["screen_event"], "승리")


if __name__ == "__main__":
    unittest.main()
