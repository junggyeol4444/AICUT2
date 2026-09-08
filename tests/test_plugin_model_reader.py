"""The adapter's side of 37장: read the Common Edit Model, never the plan.

`plugin/common/aicut_model.py` runs inside an editor's own interpreter, so it
is standard library only and cannot import aicut. These tests drive it with a
model the engine actually produced, so the two halves cannot drift apart.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin" / "common"))

import aicut_model  # noqa: E402


def _model(**overrides):
    """A model built by the engine, not written by hand for the test."""
    from aicut.render.editmodel import from_edit_plan
    from aicut.render.editplan import EditPlan

    plan_data = {
        "episode_id": "ep-abcdefgh",
        "project_id": "p1",
        "source_path": "/media/live.mkv",
        "structure": {},
        "render": {"fps": 30},
        "cuts": [
            {"sequence_order": 0, "source_start_sec": 4000.0, "source_end_sec": 4020.0,
             "scene_role": "결과", "remove_spans": [[4010.0, 4012.0]]},
            {"sequence_order": 1, "source_start_sec": 100.0, "source_end_sec": 130.0,
             "scene_role": "배경"},
        ],
        "subtitles": [{"start_sec": 1.0, "end_sec": 2.0, "text": "뭐야", "speaker": "host"}],
    }
    plan_data.update(overrides)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "plan.json"
        path.write_text(json.dumps(plan_data), encoding="utf-8")
        return from_edit_plan(EditPlan.load(path)).as_dict()


class ItRefusesThingsThatAreNotTheModelTests(unittest.TestCase):
    def test_an_edit_plan_is_named_as_such_and_the_way_out_is_given(self):
        """The mistake this file exists to prevent, caught with its own name."""
        with self.assertRaises(aicut_model.ModelError) as refused:
            aicut_model.validated({"cuts": [{"sequence_order": 0}], "episode_id": "x"})

        self.assertIn("edit plan", str(refused.exception))
        self.assertIn("edit-model", str(refused.exception))

    def test_a_model_with_no_sequence_has_no_timeline_to_build(self):
        with self.assertRaises(aicut_model.ModelError):
            aicut_model.validated({"sequences": []})

    def test_a_missing_file_says_so_rather_than_raising_an_os_error(self):
        with self.assertRaises(aicut_model.ModelError):
            aicut_model.load("/nowhere/model.json")


class TimelineOrderTests(unittest.TestCase):
    def test_clips_come_back_in_timeline_order_not_source_order(self):
        """2.4: sorting by in-point would rebuild the broadcast."""
        model = _model()
        clips = aicut_model.video_clips(model["sequences"][0])

        self.assertEqual([c["in_point_sec"] for c in clips], [4000.0, 100.0])
        self.assertEqual([c["timeline_position_sec"] for c in clips], [0.0, 18.0])


class RemovedSpansTests(unittest.TestCase):
    def test_a_removal_splits_one_clip_into_the_spans_that_survive(self):
        model = _model()
        first = aicut_model.video_clips(model["sequences"][0])[0]

        self.assertEqual(aicut_model.kept_spans(first),
                         [(4000.0, 4010.0), (4012.0, 4020.0)])

    def test_the_surviving_length_is_what_the_model_said_it_was(self):
        model = _model()
        first = aicut_model.video_clips(model["sequences"][0])[0]
        kept = sum(b - a for a, b in aicut_model.kept_spans(first))

        self.assertAlmostEqual(kept, first["timeline_duration_sec"])


class FrameArithmeticTests(unittest.TestCase):
    def test_the_end_frame_is_inclusive(self):
        """Resolve counts the last frame, not one past it. Off by one here is a
        frame added or lost on every cut, found only at export."""
        model = _model(cuts=[{"sequence_order": 0, "source_start_sec": 0.0,
                              "source_end_sec": 1.0}])
        ranges = aicut_model.frame_ranges(model["sequences"][0], 30)

        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[0][1], 29)

    def test_a_span_shorter_than_a_frame_is_reported_not_dropped_silently(self):
        model = _model(cuts=[
            {"sequence_order": 0, "source_start_sec": 0.0, "source_end_sec": 5.0},
            {"sequence_order": 1, "source_start_sec": 10.0, "source_end_sec": 10.005},
        ])
        sequence = model["sequences"][0]

        self.assertEqual(len(aicut_model.frame_ranges(sequence, 30)), 1)
        self.assertEqual(aicut_model.dropped_spans(sequence, 30), [(10.0, 10.005)])

    def test_a_frame_rate_that_is_not_positive_is_refused(self):
        model = _model()
        with self.assertRaises(aicut_model.ModelError):
            aicut_model.frame_ranges(model["sequences"][0], 0)

    def test_a_sequence_where_nothing_survives_says_so(self):
        model = _model(cuts=[{"sequence_order": 0, "source_start_sec": 0.0,
                              "source_end_sec": 0.005}])
        with self.assertRaises(aicut_model.ModelError):
            aicut_model.frame_ranges(model["sequences"][0], 30)


class TracksAreReadFromTheModelTests(unittest.TestCase):
    def test_only_the_tracks_the_model_has_come_back(self):
        """24장: 필요한 Track을 AI가 구성한다."""
        model = _model()
        sequence = model["sequences"][0]

        self.assertEqual(aicut_model.audio_clips(sequence), [])
        self.assertEqual([s["text"] for s in aicut_model.subtitles(sequence)], ["뭐야"])

    def test_music_the_plan_asked_for_comes_back_with_its_track_name(self):
        model = _model(structure={"bgm": {"path": "/a/b.mp3", "gain_db": -18}})
        placed = aicut_model.audio_clips(model["sequences"][0])

        self.assertEqual([name for name, _ in placed], ["BGM"])
        self.assertEqual(placed[0][1]["path"], "/a/b.mp3")

    def test_the_source_audio_is_not_placed_twice(self):
        """24장 gives 원본 음성 its own track, and a linked A/V clip already
        carries it; an adapter laying these down too would double it."""
        model = _model(structure={"bgm": "/a/b.mp3"})
        placed = aicut_model.audio_clips(model["sequences"][0])

        self.assertNotIn("원본 음성", [name for name, _ in placed])


class WhatTheAdapterAsksForTests(unittest.TestCase):
    def test_the_media_path_comes_from_the_model(self):
        model = _model()

        self.assertEqual(aicut_model.media_path(model, "source"), "/media/live.mkv")

    def test_an_unknown_media_id_is_named(self):
        with self.assertRaises(aicut_model.ModelError):
            aicut_model.media_path(_model(), "b-roll")

    def test_the_timeline_gets_a_findable_name(self):
        """25장 puts the AI sequence beside what the operator built."""
        model = _model()
        name = aicut_model.timeline_name(model, model["sequences"][0])

        self.assertTrue(name)
        self.assertNotEqual(name, model["sequences"][0]["sequence_id"])

    def test_the_summary_states_what_was_built(self):
        model = _model()
        line = aicut_model.summary(model, model["sequences"][0], 30)

        self.assertIn("clips", line)
        self.assertIn("live.mkv", line)
