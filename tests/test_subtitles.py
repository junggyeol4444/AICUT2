import tempfile
import unittest
from pathlib import Path

from backend.subtitles import SubtitleError, build_output_cues, write_ass_subtitles


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

    def test_maps_transcript_onto_non_linear_active_cuts(self):
        cues, duration = build_output_cues([
            {"cut_id": 1, "sequence_order": 1, "source_start_sec": 50, "source_end_sec": 55,
             "pacing_mode": "KEEP"},
            {"cut_id": 2, "sequence_order": 2, "source_start_sec": 10, "source_end_sec": 20,
             "pacing_mode": "KEEP"},
            {"cut_id": 3, "sequence_order": 3, "source_start_sec": 30, "source_end_sec": 40,
             "pacing_mode": "CUT"},
        ], [
            {"start_sec": 8, "end_sec": 12, "speaker_tag": "HOST", "text": "배경 설명"},
            {"start_sec": 18, "end_sec": 22, "speaker_tag": "HOST", "text": "다음 이야기"},
            {"start_sec": 31, "end_sec": 32, "speaker_tag": "HOST", "text": "삭제된 대사"},
            {"start_sec": 49, "end_sec": 52, "speaker_tag": "GUEST", "text": "결과 장면"},
        ])
        self.assertEqual(duration, 15)
        self.assertEqual([cue["text"] for cue in cues], ["결과 장면", "배경 설명", "다음 이야기"])
        self.assertEqual(
            [(cue["start_sec"], cue["end_sec"]) for cue in cues],
            [(0, 2), (5, 7), (13, 15)],
        )
        self.assertEqual([cue["cut_id"] for cue in cues], [1, 2, 2])

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
