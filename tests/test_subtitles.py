import tempfile
import unittest
from pathlib import Path

from backend.subtitles import SubtitleError, write_ass_subtitles


class SubtitleTest(unittest.TestCase):
    def setUp(self):
        self.style = {
            "font_name": "Open Sans", "font_size": 52,
            "primary_color": "&H00FFFFFF", "outline_color": "&H00000000", "margin_v": 80,
        }

    def test_writes_profile_driven_ass_on_output_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_ass_subtitles([
                {"start_sec": 0.25, "end_sec": 2.5, "speaker_tag": "HOST", "text": "첫 줄\n둘째 줄"},
            ], self.style, Path(directory) / "episode.ass", 10)
            content = Path(path).read_text(encoding="utf-8-sig")
        self.assertIn("Style: Default,Open Sans,52", content)
        self.assertIn("Dialogue: 0,0:00:00.25,0:00:02.50,Default,HOST", content)
        self.assertIn(r"첫 줄\N둘째 줄", content)

    def test_rejects_hardcoded_or_out_of_range_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SubtitleError):
                write_ass_subtitles([], {}, Path(directory) / "missing-profile.ass", 10)
            with self.assertRaises(SubtitleError):
                write_ass_subtitles([
                    {"start_sec": 9, "end_sec": 11, "text": "outside"},
                ], self.style, Path(directory) / "outside.ass", 10)


if __name__ == "__main__":
    unittest.main()
