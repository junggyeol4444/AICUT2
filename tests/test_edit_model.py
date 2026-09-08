"""The Common Edit Model (플러그인 기획안 37장, 38장; MVP 5).

37장 puts a layer between the AI Engine and any editor's API, and 38장 names
what is in it. These hold that boundary: the model says what the plan said, in
editor-independent terms, and decides nothing of its own (39장 / 40장).
"""

import json
import tempfile
import unittest
from pathlib import Path


def _plan(**overrides):
    from aicut.render.editplan import EditPlan

    data = {
        "episode_id": "ep-abcdefgh",
        "project_id": "p1",
        "source_path": "/media/live.mkv",
        "target_type": "장편",
        "structure": {"structure_name": "결과 먼저"},
        "render": {"fps": 30, "width": 1920, "height": 1080, "transition_sec": 0.5},
        "cuts": [
            {"sequence_order": 0, "source_start_sec": 4000.0, "source_end_sec": 4020.0,
             "scene_role": "결과", "speaker_tag": "host"},
            {"sequence_order": 1, "source_start_sec": 100.0, "source_end_sec": 130.0,
             "scene_role": "배경"},
        ],
        "subtitles": [],
    }
    data.update(overrides)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "plan.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return EditPlan.load(path)


class NonLinearOrderSurvivesTests(unittest.TestCase):
    """2.4: 완성본 순서와 원본 위치는 무관하다. The model keeps them apart."""

    def test_a_later_moment_shown_first_stays_that_way(self):
        from aicut.render.editmodel import from_edit_plan

        clips = from_edit_plan(_plan()).sequences[0].tracks[0].clips

        self.assertEqual([c.in_point_sec for c in clips], [4000.0, 100.0])
        self.assertEqual([c.timeline_position_sec for c in clips], [0.0, 20.0])

    def test_the_timeline_position_is_never_derived_from_the_source(self):
        """An adapter that sorted by in-point would rebuild the broadcast."""
        from aicut.render.editmodel import from_edit_plan

        clips = from_edit_plan(_plan()).sequences[0].tracks[0].clips
        by_source = sorted(clips, key=lambda c: c.in_point_sec)

        self.assertNotEqual([c.clip_id for c in clips], [c.clip_id for c in by_source])


class RemovedSpansDoNotOverlapTests(unittest.TestCase):
    """A cut with a pacing removal spans more source than timeline.

    An adapter placing `duration_sec` of source at `timeline_position_sec`
    overlaps the next clip by exactly the removed length, so both numbers are
    on the model and the difference is stated.
    """

    def _clips(self):
        from aicut.render.editmodel import from_edit_plan

        plan = _plan(cuts=[
            {"sequence_order": 0, "source_start_sec": 0.0, "source_end_sec": 20.0,
             "remove_spans": [[10.0, 12.0]]},
            {"sequence_order": 1, "source_start_sec": 100.0, "source_end_sec": 130.0},
        ])
        return from_edit_plan(plan).sequences[0].tracks[0].clips

    def test_the_source_span_and_the_timeline_span_are_both_stated(self):
        first = self._clips()[0]

        self.assertEqual(first.duration_sec, 20.0)
        self.assertEqual(first.timeline_duration_sec, 18.0)

    def test_laying_clips_out_by_the_timeline_span_leaves_no_overlap(self):
        clips = self._clips()

        for earlier, later in zip(clips, clips[1:]):
            self.assertLessEqual(
                earlier.timeline_position_sec + earlier.timeline_duration_sec,
                later.timeline_position_sec + 1e-6,
            )

    def test_the_removal_is_carried_rather_than_pre_applied(self):
        """9.3's judgement is about this cut; an editor performs it."""
        self.assertEqual(self._clips()[0].remove_spans, [[10.0, 12.0]])


class TracksAreWhatThePlanNeedsTests(unittest.TestCase):
    """24장: 필요한 Track을 AI가 구성한다. Its six-track list is an example."""

    def _names(self, plan):
        from aicut.render.editmodel import from_edit_plan

        return [t.name for t in from_edit_plan(plan).sequences[0].tracks]

    def test_a_plan_with_no_music_gets_no_music_track(self):
        names = self._names(_plan())

        self.assertNotIn("BGM", names)
        self.assertNotIn("효과음", names)
        self.assertNotIn("자막", names)

    def test_music_in_the_plan_creates_the_track_that_holds_it(self):
        plan = _plan(structure={"structure_name": "x",
                                "bgm": {"path": "/a/b.mp3", "gain_db": -18, "loop": True}})
        names = self._names(plan)

        self.assertIn("BGM", names)

    def test_a_sound_effect_lands_at_its_time_on_the_finished_timeline(self):
        """The plan states the moment within the cut; the track is the sequence."""
        from aicut.render.editmodel import from_edit_plan

        plan = _plan(cuts=[
            {"sequence_order": 0, "source_start_sec": 0.0, "source_end_sec": 20.0},
            {"sequence_order": 1, "source_start_sec": 100.0, "source_end_sec": 130.0,
             "audio_effect": {"sfx": {"path": "/a/boom.wav", "at": 3.0}}},
        ])
        model = from_edit_plan(plan)
        sfx = [t for t in model.sequences[0].tracks if t.name == "효과음"][0]

        # Second cut starts at 20s on the timeline; the effect is 3s into it.
        self.assertEqual(sfx.audio[0].timeline_position_sec, 23.0)

    def test_subtitles_get_their_own_track_only_when_there_are_any(self):
        from aicut.render.editmodel import from_edit_plan

        plan = _plan(subtitles=[{"start_sec": 0.5, "end_sec": 2.0, "text": "뭐야",
                                 "speaker": "host", "emphasis": True}])
        tracks = {t.name: t for t in from_edit_plan(plan).sequences[0].tracks}

        self.assertIn("자막", tracks)
        self.assertEqual(tracks["자막"].subtitles[0].text, "뭐야")
        self.assertTrue(tracks["자막"].subtitles[0].emphasis)

    def test_tracks_are_numbered_within_their_own_kind(self):
        """So an adapter can map them onto V1/V2 or A1/A2 without this layer
        knowing either naming."""
        from aicut.render.editmodel import from_edit_plan

        plan = _plan(structure={"bgm": "/a/b.mp3"}, cuts=[
            {"sequence_order": 0, "source_start_sec": 0.0, "source_end_sec": 10.0,
             "audio_effect": {"sfx": "/a/s.wav"}},
        ])
        audio = [t for t in from_edit_plan(plan).sequences[0].tracks if t.kind == "audio"]

        self.assertEqual(sorted(t.index for t in audio), list(range(1, len(audio) + 1)))


class TheModelDecidesNothingTests(unittest.TestCase):
    """39장 keeps 장면 선택 / 순서 / 효과 사용 여부 with the AI; 40장 leaves this
    layer Timeline 생성 and Clip 배치. It restates, it does not choose."""

    def test_an_effect_the_plan_did_not_ask_for_is_not_added(self):
        from aicut.render.editmodel import from_edit_plan

        for clip in from_edit_plan(_plan()).sequences[0].tracks[0].clips:
            self.assertEqual(clip.effects, [])
            self.assertEqual(clip.transitions, [])

    def test_a_transition_length_comes_from_the_plan_not_from_here(self):
        from aicut.render.editmodel import from_edit_plan

        plan = _plan(
            render={"fps": 30, "transition_sec": 1.25},
            cuts=[{"sequence_order": 0, "source_start_sec": 0.0, "source_end_sec": 10.0,
                   "visual_effect": {"transition": "fade"}}],
        )
        transitions = from_edit_plan(plan).sequences[0].tracks[0].clips[0].transitions

        self.assertEqual({t.duration_sec for t in transitions}, {1.25})

    def test_a_transition_stated_for_one_edge_stays_on_that_edge(self):
        from aicut.render.editmodel import from_edit_plan

        plan = _plan(cuts=[{"sequence_order": 0, "source_start_sec": 0.0,
                            "source_end_sec": 10.0,
                            "visual_effect": {"transition": {"in": "fade"}}}])
        transitions = from_edit_plan(plan).sequences[0].tracks[0].clips[0].transitions

        self.assertEqual([(t.kind, t.edge) for t in transitions], [("fade", "in")])

    def test_the_plans_own_words_for_an_effect_are_kept(self):
        from aicut.render.editmodel import from_edit_plan

        plan = _plan(cuts=[{"sequence_order": 0, "source_start_sec": 0.0,
                            "source_end_sec": 10.0,
                            "visual_effect": {"type": "zoom", "scale": 0.8,
                                              "center": [0.4, 0.5], "crop": "9:16"}}])
        effects = from_edit_plan(plan).sequences[0].tracks[0].clips[0].effects

        self.assertEqual([e.kind for e in effects], ["zoom", "crop"])
        self.assertEqual(effects[0].params["scale"], 0.8)


class TimelineModeTests(unittest.TestCase):
    """25장: 기본값은 원본 보호를 위해 새로운 AI Sequence 생성을 권장한다."""

    def test_the_default_leaves_what_the_operator_built_alone(self):
        from aicut.render.editmodel import from_edit_plan

        self.assertEqual(from_edit_plan(_plan()).mode, "new_sequence")

    def test_editing_the_current_timeline_is_chosen_explicitly(self):
        from aicut.render.editmodel import from_edit_plan

        model = from_edit_plan(_plan(), mode="edit_current")

        self.assertEqual(model.mode, "edit_current")

    def test_a_mode_the_clause_does_not_have_is_refused(self):
        from aicut.render.editmodel import ProjectModel

        with self.assertRaises(ValueError):
            ProjectModel(name="x", mode="overwrite_everything")


class WhatTheEditorNeedsToKnowTests(unittest.TestCase):
    def test_the_sequence_carries_the_plans_frame_rate_and_size(self):
        """A sequence at a different frame rate slides every cut."""
        from aicut.render.editmodel import from_edit_plan

        sequence = from_edit_plan(_plan()).sequences[0]

        self.assertEqual(sequence.fps, 30)
        self.assertEqual((sequence.width, sequence.height), (1920, 1080))

    def test_each_cut_gets_a_marker_where_it_begins(self):
        """29장 has the person check the result; the cuts are where they step."""
        from aicut.render.editmodel import from_edit_plan

        markers = from_edit_plan(_plan()).sequences[0].markers

        self.assertEqual([m.at_sec for m in markers], [0.0, 20.0])
        self.assertEqual([m.name for m in markers], ["결과", "배경"])

    def test_the_model_round_trips_through_json(self):
        from aicut.render.editmodel import from_edit_plan

        model = from_edit_plan(_plan())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            written = model.save(Path(tmp) / "model.json")
            back = json.loads(written.read_text(encoding="utf-8"))

        self.assertEqual(back["mode"], "new_sequence")
        self.assertEqual(back["sequences"][0]["tracks"][0]["clips"][0]["in_point_sec"], 4000.0)

    def test_the_provenance_travels_with_the_timeline(self):
        """17.5: a timeline built on unmeasured thresholds has to say so where
        the person doing the finishing will see it."""
        from aicut.render.editmodel import from_edit_plan

        plan = _plan()
        plan.provenance = {"provisional_parameters": ["pacing"]}
        notes = from_edit_plan(plan).sequences[0].notes

        self.assertEqual(notes["provenance"], {"provisional_parameters": ["pacing"]})
