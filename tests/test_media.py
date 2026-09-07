import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.media import DiskCapacityError, MediaProbeError, check_disk_capacity, probe_media


class MediaProbeTest(unittest.TestCase):
    def test_ffprobe_streams_are_normalized(self):
        with tempfile.NamedTemporaryFile(suffix=".mkv") as source, patch("backend.media.shutil.which", return_value="/usr/bin/ffprobe"):
            payload = {
                "format": {"duration": "3600.25", "size": "1000", "format_name": "matroska"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "60000/1001"},
                    {"codec_type": "audio", "codec_name": "aac"},
                    {"codec_type": "audio", "codec_name": "opus"},
                ],
            }
            runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
            info = probe_media(source.name, runner=runner)
        self.assertEqual((info.width, info.height, info.audio_tracks), (1920, 1080, 2))
        self.assertAlmostEqual(info.frame_rate, 59.94, places=2)
        self.assertEqual(info.audio_codecs, ("aac", "opus"))

    def test_missing_source_is_rejected_before_process_execution(self):
        with self.assertRaises(MediaProbeError):
            probe_media(Path("/definitely/missing/video.mkv"))

    def test_disk_capacity_reserves_space_before_long_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            result = check_disk_capacity(Path(directory) / "future" / "artifacts", 1, 1)
            self.assertGreaterEqual(result["available_bytes"], 1)
            with self.assertRaises(DiskCapacityError):
                check_disk_capacity(directory, result["free_bytes"] + 1)
