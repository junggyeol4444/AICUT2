import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.multimodal import analyze_audio_tracks, analyze_video


class MultimodalAnalysisTest(unittest.TestCase):
    def test_audio_features_are_timestamped_per_track(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.wav"
            samples = [round(16000 * math.sin(2 * math.pi * index / 20)) for index in range(100)]
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 100, 0, "NONE", ""))
                output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            result = analyze_audio_tracks([path], 1.0, window_sec=0.5)
            self.assertEqual(len(result["observations"]), 2)
            self.assertGreater(result["observations"][0]["payload"]["zero_crossing_rate"], 0)
            self.assertLess(result["observations"][0]["payload"]["rms_dbfs"], 0)

    def test_audio_precision_ranges_seek_without_analyzing_the_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.wav"
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 100, 0, "NONE", ""))
                output.writeframes(struct.pack("<1000h", *([1000] * 1000)))
            result = analyze_audio_tracks([path], 10, window_sec=0.25, ranges=[
                {"start_sec": 2, "end_sec": 2.5, "reason": "low_stt_confidence"},
                {"start_sec": 8, "end_sec": 8.25, "reason": "vision_scene_change"},
            ])
            self.assertEqual([(item["start_sec"], item["end_sec"]) for item in result["observations"]],
                             [(2, 2.25), (2.25, 2.5), (8, 8.25)])
            self.assertEqual(result["observations"][-1]["payload"]["selection_reason"], "vision_scene_change")

    @patch("backend.multimodal.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_video_metadata_is_converted_to_source_timeline(self, _which):
        output = "\n".join(["frame:0 pts:0 pts_time:0", "lavfi.signalstats.YAVG=81.5",
                            "frame:1 pts:5 pts_time:5", "lavfi.signalstats.YAVG=90"])
        runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output, stderr="")
        result = analyze_video("/media/live.mkv", 8, frame_interval_sec=5, runner=runner)
        self.assertEqual([(x["start_sec"], x["end_sec"]) for x in result["observations"]], [(0, 5), (5, 8)])
        self.assertEqual(result["observations"][0]["payload"]["signalstats.YAVG"], 81.5)


if __name__ == "__main__":
    unittest.main()
