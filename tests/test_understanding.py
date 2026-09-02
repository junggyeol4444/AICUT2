import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.database import Database
from backend.understanding import (
    PreprocessPlan, UnderstandingError, build_preprocess_commands, build_scan_plan,
    execute_preprocess, validate_transcript_segments,
    select_precision_ranges,
)


class BroadcastUnderstandingTest(unittest.TestCase):
    def test_preprocess_plan_keeps_each_audio_track_separate(self):
        plan = PreprocessPlan("/media/live.mkv", "/artifacts/project", 4, 12)
        commands = build_preprocess_commands(plan)
        self.assertEqual(len(commands["audio"]), 4)
        self.assertEqual([command[command.index("-map") + 1] for command in commands["audio"]], [
            "0:a:0", "0:a:1", "0:a:2", "0:a:3",
        ])
        self.assertIn("fps=1/12", commands["frames"])

    def test_preprocess_executes_audio_and_frame_artifacts(self):
        calls = []
        runner = lambda command, **kwargs: (calls.append(command) or SimpleNamespace(returncode=0, stderr=""))
        with tempfile.TemporaryDirectory() as directory, patch("backend.understanding.shutil.which", return_value="/usr/bin/ffmpeg"):
            result = execute_preprocess(PreprocessPlan("source.mkv", directory, 2, 10), runner)
        self.assertEqual(len(calls), 3)
        self.assertEqual([item["kind"] for item in result["artifacts"]], ["AUDIO", "AUDIO", "FRAMES"])

    def test_coarse_scan_covers_entire_broadcast_without_gaps(self):
        windows = build_scan_plan(65, 20)
        coarse = [window for window in windows if window.pass_kind == "COARSE"]
        self.assertEqual([(window.start_sec, window.end_sec) for window in coarse], [
            (0, 20), (20, 40), (40, 60), (60, 65),
        ])

    def test_overlapping_precision_ranges_are_merged(self):
        windows = build_scan_plan(100, 25, [
            {"start_sec": 40, "end_sec": 55, "reason": "audio"},
            {"start_sec": 50, "end_sec": 70, "reason": "topic"},
        ])
        precision = [window for window in windows if window.pass_kind == "PRECISION"]
        self.assertEqual(len(precision), 1)
        self.assertEqual((precision[0].start_sec, precision[0].end_sec), (40, 70))

    def test_word_timestamps_must_stay_inside_segment_and_source(self):
        valid = validate_transcript_segments([{
            "track_index": 0, "start_sec": 10, "end_sec": 12, "text": "안녕하세요",
            "confidence": .95, "words": [{"start_sec": 10, "end_sec": 11, "word": "안녕"}],
        }], 100)
        self.assertEqual(valid[0]["speaker_tag"], "UNKNOWN")
        with self.assertRaises(UnderstandingError):
            validate_transcript_segments([{
                "start_sec": 10, "end_sec": 12, "text": "오류",
                "words": [{"start_sec": 9, "end_sec": 11, "word": "오류"}],
            }], 100)

    def test_precision_ranges_use_calibrated_multimodal_signals_and_merge_context(self):
        ranges = select_precision_ranges(100, [{
            "start_sec": 10, "end_sec": 12, "confidence": 0.4,
        }], [{
            "modality": "AUDIO", "kind": "SIGNAL_WINDOW", "track_index": 0,
            "start_sec": 12, "end_sec": 13, "payload": {"rms_dbfs": -30},
        }, {
            "modality": "AUDIO", "kind": "SIGNAL_WINDOW", "track_index": 0,
            "start_sec": 13, "end_sec": 14, "payload": {"rms_dbfs": -10},
        }, {
            "modality": "VISION", "kind": "FRAME_SIGNAL", "start_sec": 50, "end_sec": 55,
            "payload": {"scd.score": 0.8},
        }], {
            "context_before_sec": 2, "context_after_sec": 3, "stt_confidence_below": 0.5,
            "audio_rms_delta_db": 15, "vision_scene_score_above": 0.7,
        })
        self.assertEqual([(item["start_sec"], item["end_sec"]) for item in ranges], [(8, 17), (48, 58)])
        self.assertEqual(ranges[0]["reason"], "low_stt_confidence,audio_rms_change")

    def test_precision_policy_requires_context_values(self):
        with self.assertRaises(UnderstandingError):
            select_precision_ranges(10, [], [], {})

    def test_scan_and_transcript_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "understanding.db")
            project = database.create_project({"file_path": "/media/live.mkv", "duration_sec": 100})
            windows = [window.__dict__ for window in build_scan_plan(100, 25)]
            self.assertEqual(database.replace_scan_windows(project["project_id"], windows), 4)
            segments = validate_transcript_segments([{
                "start_sec": 1, "end_sec": 2, "text": "테스트", "words": [],
            }], 100)
            self.assertEqual(database.replace_transcript(project["project_id"], segments), 1)

    def test_stt_audio_and_vision_share_one_ordered_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "understanding.db")
            project = database.create_project({"file_path": "/media/live.mkv", "duration_sec": 20})
            database.replace_transcript(project["project_id"], validate_transcript_segments([{
                "start_sec": 5, "end_sec": 7, "text": "대사", "confidence": .9, "words": [],
            }], 20))
            database.replace_observations(project["project_id"], "AUDIO", [{
                "kind": "SIGNAL_WINDOW", "track_index": 0, "start_sec": 0, "end_sec": 1,
                "payload": {"rms_dbfs": -10},
            }])
            database.replace_observations(project["project_id"], "VISION", [{
                "kind": "FRAME_SIGNAL", "start_sec": 5, "end_sec": 6, "payload": {"scd.score": .8},
            }])
            timeline = database.analysis_input(project["project_id"])["timeline"]
            self.assertEqual([item["modality"] for item in timeline], ["AUDIO", "VISION", "STT"])
            self.assertEqual(timeline[-1]["payload"]["text"], "대사")


if __name__ == "__main__":
    unittest.main()
