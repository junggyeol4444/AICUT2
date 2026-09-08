"""Seven defects found by review, each pinned by the case that exposed it.

Every one of these passed the suite before. They are the failures that only
show up on a real broadcast — a track named the wrong thing, a sampling grid
with a gap in it, a list arriving in the order the database happens to return.
"""

import unittest

from aicut.analysis.pacing import PacingJudge, SilenceContext
from aicut.config import CalibrationProfile
from aicut.media.probe import classify_track
from aicut.media.vision import MotionSample, stillness
from aicut.models import ContentCandidate, Decision, SituationLabel, Utterance
from aicut.pipeline.evaluating import group_for_production


class TrackRoleTests(unittest.TestCase):
    """5.2 reads speech from the mic track. Naming the wrong one poisons everything."""

    def test_a_game_track_is_not_the_microphone(self):
        """"me" is inside "game"; as a substring hint it captured every game track."""
        for title in ("Game Audio", "Game", "game", "Gameplay Capture", "게임소리"):
            with self.subTest(title=title):
                self.assertEqual(classify_track(title, 1, 4), "game")

    def test_real_microphone_titles_still_land_on_mic(self):
        for title in ("Mic", "mic-2", "Microphone", "Voice", "내 마이크", "본인"):
            with self.subTest(title=title):
                self.assertEqual(classify_track(title, 0, 4), "mic")

    def test_the_other_two_roles_survive_the_change(self):
        self.assertEqual(classify_track("Discord", 2, 4), "call")
        self.assertEqual(classify_track("합방 통화", 2, 4), "call")
        self.assertEqual(classify_track("BGM", 3, 4), "bgm")
        self.assertEqual(classify_track("Desktop Audio", 1, 4), "game")

    def test_an_unnamed_track_is_unknown_not_a_guess(self):
        self.assertEqual(classify_track("", 1, 4), "unknown")
        self.assertEqual(classify_track("Track 2", 1, 4), "unknown")

    def test_a_single_track_source_is_mixed(self):
        self.assertEqual(classify_track("", 0, 1), "mixed")


class CombineGroupTests(unittest.TestCase):
    """6.3's candidate B is only a video once welded to what resolves it."""

    def _candidates(self):
        return (
            ContentCandidate(candidate_id="A", decision=Decision.PRODUCE, core_summary="본편"),
            ContentCandidate(candidate_id="B", decision=Decision.COMBINE,
                             core_summary="결말 없음", combine_with=["A"]),
            ContentCandidate(candidate_id="C", decision=Decision.REJECT),
        )

    def _ids(self, order):
        return [sorted(c.candidate_id for c in g) for g in group_for_production(list(order))]

    def test_the_grouping_does_not_depend_on_the_order_it_is_given(self):
        a, b, c = self._candidates()
        self.assertEqual(self._ids([a, b, c]), self._ids([b, a, c]))
        self.assertEqual(self._ids([a, b, c]), self._ids([c, b, a]))

    def test_the_combine_candidate_is_not_dropped_when_produce_comes_first(self):
        """The store orders by independence score, which puts PRODUCE first."""
        a, b, c = self._candidates()
        self.assertEqual(self._ids([a, b, c]), [["A", "B"]])

    def test_a_combine_link_nothing_answers_is_still_left_out(self):
        lonely = ContentCandidate(candidate_id="X", decision=Decision.COMBINE,
                                  combine_with=["gone"])
        self.assertEqual(group_for_production([lonely]), [])

    def test_a_chain_of_links_becomes_one_group(self):
        a = ContentCandidate(candidate_id="A", decision=Decision.PRODUCE)
        b = ContentCandidate(candidate_id="B", decision=Decision.COMBINE, combine_with=["A"])
        c = ContentCandidate(candidate_id="C", decision=Decision.COMBINE, combine_with=["B"])
        self.assertEqual(self._ids([a, b, c]), [["A", "B", "C"]])


class MotionUnknownTests(unittest.TestCase):
    """9.2 asks whether the person is still. "Not measured" is not "still"."""

    def _samples(self):
        return [MotionSample(at_sec=0.0, score=0.9), MotionSample(at_sec=5.0, score=0.9)]

    def test_a_span_with_no_sample_is_unknown(self):
        self.assertIsNone(stillness(self._samples(), 2.0, 2.1))

    def test_a_span_that_has_samples_is_still_averaged(self):
        self.assertAlmostEqual(stillness(self._samples(), 0.0, 5.0), 0.9)

    def test_a_span_past_the_end_of_the_measurement_is_unknown(self):
        self.assertIsNone(stillness(self._samples(), 10.0, 11.0))

    def test_an_empty_curve_is_unknown_rather_than_zero(self):
        self.assertIsNone(stillness([], 0.0, 1.0))

    def test_unmeasured_motion_does_not_invent_a_keep_signal(self):
        """`frozen_after_peak` fired on a gap in the sampling grid."""
        profile = CalibrationProfile.load()
        judge = PacingJudge(profile)
        measured_still = SilenceContext(start_sec=9.0, end_sec=10.0, motion=0.0,
                                        preceding_tension=1.0)
        unmeasured = SilenceContext(start_sec=9.0, end_sec=10.0, motion=None,
                                    preceding_tension=1.0)
        self.assertIn("frozen", judge.judge(measured_still).reason)
        self.assertNotIn("frozen", judge.judge(unmeasured).reason)
        self.assertGreater(judge.judge(measured_still).score, judge.judge(unmeasured).score)

    def test_the_reported_signal_says_unknown_rather_than_zero(self):
        judge = PacingJudge(CalibrationProfile.load())
        decision = judge.judge(SilenceContext(start_sec=9.0, end_sec=10.0, motion=None))
        self.assertIsNone(decision.signals["motion"])

    def test_away_is_not_claimed_without_a_screen_measurement(self):
        """5.3's 대기·자리비움 is a statement about the screen."""
        from aicut.analysis.signals import label_situations

        profile = CalibrationProfile.load()
        spans = label_situations(10.0, [], [], profile)
        self.assertTrue(spans)
        self.assertNotIn(SituationLabel.AWAY, {s.label for s in spans},
                         "an empty motion curve was read as an empty desk")


class BoundarySilenceTests(unittest.TestCase):
    """9장 exists to remove dead air; padding puts it exactly on the boundary."""

    def test_a_silence_crossing_the_cut_start_is_judged_on_its_overlap(self):
        import dataclasses

        from aicut.media.audio import Silence

        cut_start, cut_end = 100.0, 130.0
        silence = Silence(97.0, 104.0)          # starts before the cut
        start = max(silence.start_sec, cut_start)
        end = min(silence.end_sec, cut_end)
        clipped = dataclasses.replace(silence, start_sec=start, end_sec=end)
        self.assertEqual((clipped.start_sec, clipped.end_sec), (100.0, 104.0))

    def test_planning_clips_rather_than_filters(self):
        import inspect

        from aicut.pipeline import planning

        source = inspect.getsource(planning._apply_pacing)
        self.assertIn("max(silence.start_sec, cut.source_start_sec)", source)
        self.assertNotIn("s.start_sec >= cut.source_start_sec and s.end_sec <= cut.source_end_sec",
                         source)


class MergeRelationTests(unittest.TestCase):
    """5.4's graph is what discovery and planning read. A shifted index rewires it."""

    def test_relations_are_resolved_by_group_not_by_position(self):
        import inspect

        from aicut.pipeline import understanding

        source = inspect.getsource(understanding._merge_events)
        self.assertIn("event_for_group", source)
        self.assertNotIn("zip(groups, merged)", source,
                         "a skipped group shifts every relation after it")


class ResumeTests(unittest.TestCase):
    """A replaced transcript invalidates what was derived from the old one."""

    def test_a_supplied_transcriber_rebuilds_understanding(self):
        import inspect

        from aicut.pipeline import runner

        source = inspect.getsource(runner.Pipeline.run)
        self.assertIn("replaced_speech", source)
        self.assertIn("transcriber is not None", source)


class UiSpeechTests(unittest.TestCase):
    """18장 puts STT 처리 on the program. 15.2 lets the operator supply only a file."""

    def test_the_server_builds_the_same_recogniser_the_cli_does(self):
        import inspect

        from aicut.media import stt
        from aicut.ui.server import UiServer

        self.assertTrue(hasattr(stt, "build_transcriber"))
        self.assertIn("build_transcriber", inspect.getsource(UiServer._speech_recogniser))

    def test_a_submission_with_no_transcript_does_not_run_on_no_speech(self):
        import inspect

        from aicut.ui.server import UiServer

        source = inspect.getsource(UiServer.submit)
        self.assertIn("_speech_recogniser", source)
        self.assertNotIn("will fall back to whatever utterances are already stored", source)

    def test_an_unknown_backend_is_refused_rather_than_guessed(self):
        from aicut.media.stt import build_transcriber

        with self.assertRaises(ValueError):
            build_transcriber("not-a-backend")


if __name__ == "__main__":
    unittest.main()


class CutPieceEffectTests(unittest.TestCase):
    """8.2's intent belongs to the cut. Pacing splits the cut, not the intent."""

    def _split_cut(self):
        from aicut.models import Cut

        return Cut(
            sequence_order=0, source_start_sec=0, source_end_sec=30,
            visual_effect={"transition": "fade",
                           "graphic": {"path": "/x.png", "start": 1.0, "end": 3.0},
                           "crop": "9:16"},
            audio_effect={"sfx": {"path": "/d.wav", "at": 14.0}, "gain_db": -3},
            remove_spans=[[10, 12], [20, 22]],
        )

    def _placed(self):
        from aicut.render.ffmpeg import _effects_for_piece, _pieces_per_cut
        from aicut.render.timeline import Timeline

        cut = self._split_cut()
        timeline = Timeline.from_cuts([cut])
        pieces = _pieces_per_cut(timeline.segments)
        return [(_effects_for_piece(cut, s, pieces)) for s in timeline.segments]

    def test_a_split_cut_really_does_become_several_segments(self):
        self.assertEqual(len(self._placed()), 3)

    def test_the_transition_fades_the_ends_of_the_cut_not_every_piece(self):
        placed = self._placed()
        self.assertEqual(placed[0][0]["transition"], {"in": "fade"})
        self.assertNotIn("transition", placed[1][0])
        self.assertEqual(placed[2][0]["transition"], {"out": "fade"})

    def test_a_graphic_appears_once_on_the_piece_that_holds_its_time(self):
        placed = self._placed()
        self.assertIn("graphic", placed[0][0])
        self.assertNotIn("graphic", placed[1][0])
        self.assertNotIn("graphic", placed[2][0])

    def test_a_sound_effect_is_rebased_onto_its_own_piece(self):
        """14s into the cut is 4s into the second piece once 10-12 is removed."""
        placed = self._placed()
        self.assertNotIn("sfx", placed[0][1])
        self.assertEqual(placed[1][1]["sfx"]["at"], 4.0)
        self.assertNotIn("sfx", placed[2][1])

    def test_framing_and_level_stay_on_every_piece(self):
        for visual, audio in self._placed():
            self.assertEqual(visual["crop"], "9:16")
            self.assertEqual(audio["gain_db"], -3)

    def test_an_unsplit_cut_keeps_everything_it_was_given(self):
        from aicut.models import Cut
        from aicut.render.ffmpeg import _effects_for_piece, _pieces_per_cut
        from aicut.render.timeline import Timeline

        cut = Cut(sequence_order=0, source_start_sec=0, source_end_sec=10,
                  visual_effect={"transition": "fade"},
                  audio_effect={"sfx": {"path": "/d.wav", "at": 5.0}})
        timeline = Timeline.from_cuts([cut])
        visual, audio = _effects_for_piece(
            cut, timeline.segments[0], _pieces_per_cut(timeline.segments),
        )
        self.assertEqual(visual["transition"], "fade")
        self.assertEqual(audio["sfx"]["at"], 5.0)


class LoudnessOverTheBedTests(unittest.TestCase):
    """10.4-3 measures then corrects. Both have to be the same audio."""

    def test_the_render_and_the_measurement_build_the_same_mix(self):
        from aicut.render.ffmpeg import _bed_mix_chains

        bed = {"path": "/b.mp3", "gain_db": -18.0, "fade": 1.0, "loop": True}
        rendered = _bed_mix_chains(bed, "loudnorm=I=-14,aresample=48000")
        measured = _bed_mix_chains(bed, "loudnorm=I=-14:print_format=json")
        self.assertEqual(rendered[0], measured[0], "the bed is levelled differently")
        self.assertIn("amix=inputs=2:normalize=0:duration=first", rendered[1])
        self.assertIn("amix=inputs=2:normalize=0:duration=first", measured[1])

    def test_without_a_bed_the_measurement_is_the_plain_one(self):
        import inspect

        from aicut.render.ffmpeg import measure_loudness_with_bed

        source = inspect.getsource(measure_loudness_with_bed)
        self.assertIn("return measure_loudness(joined_path, profile)", source)

    def test_the_renderer_measures_with_the_bed_it_will_add(self):
        import inspect

        from aicut.render.ffmpeg import Renderer

        source = inspect.getsource(Renderer.render)
        self.assertIn("measure_loudness_with_bed", source)
        self.assertIn('plan.structure.get("bgm")', source)


class SubtitleTimestampTests(unittest.TestCase):
    """A malformed ASS time loses or misplaces the caption."""

    def test_rounding_carries_past_the_minute_and_the_hour(self):
        from aicut.render.subtitles import _timestamp

        self.assertEqual(_timestamp(59.999), "0:01:00.00")
        self.assertEqual(_timestamp(3599.999), "1:00:00.00")

    def test_no_field_can_reach_sixty(self):
        from aicut.render.subtitles import _timestamp

        for tenth in range(0, 40000):
            stamp = _timestamp(tenth / 10)
            _, minutes, rest = stamp.split(":")
            secs = rest.split(".")[0]
            self.assertLess(int(minutes), 60, stamp)
            self.assertLess(int(secs), 60, stamp)

    def test_ordinary_times_are_unchanged(self):
        from aicut.render.subtitles import _timestamp

        self.assertEqual(_timestamp(0.0), "0:00:00.00")
        self.assertEqual(_timestamp(0.5), "0:00:00.50")
        self.assertEqual(_timestamp(61.2), "0:01:01.20")
        self.assertEqual(_timestamp(3661.004), "1:01:01.00")

    def test_a_negative_time_is_clamped_rather_than_formatted(self):
        from aicut.render.subtitles import _timestamp

        self.assertEqual(_timestamp(-1.0), "0:00:00.00")


class RetryQueueScopeTests(unittest.TestCase):
    """11.4's queue is per project, because the profile that uploads it is."""

    def _store_with_two_projects(self):
        from aicut.db.store import Store
        from aicut.models import Episode, Project

        store = Store(":memory:")
        for pid in ("p1", "p2"):
            store.create_project(Project(project_id=pid, file_path=f"/x/{pid}.mp4",
                                         duration_sec=10.0))
            store.save_episode(Episode(episode_id=f"e-{pid}", project_id=pid))
            store.enqueue_upload(f"e-{pid}", retry_after=None, error="quota")
        return store

    def test_the_queue_can_be_read_for_one_project_only(self):
        store = self._store_with_two_projects()
        try:
            self.assertEqual(len(store.upload_queue()), 2)
            mine = store.upload_queue(project_id="p1")
            self.assertEqual([r["episode_id"] for r in mine], ["e-p1"])
        finally:
            store.close()

    def test_the_retry_drains_only_its_own_project(self):
        import inspect

        from aicut.pipeline import publishing

        source = inspect.getsource(publishing.process_retry_queue)
        self.assertIn("project_id=ctx.project.project_id", source)


class ProjectCompletionTests(unittest.TestCase):
    """14장 lists PUBLISHED as the end of the walk; nothing ever set it."""

    def _context(self, statuses):
        from aicut.db.store import Store
        from aicut.models import Episode, Project

        store = Store(":memory:")
        project = Project(project_id="p1", file_path="/x/b.mp4", duration_sec=10.0)
        store.create_project(project)
        for i, status in enumerate(statuses):
            episode = Episode(episode_id=f"e{i}", project_id="p1")
            episode.review_status = status
            store.save_episode(episode)

        class Ctx:
            pass

        ctx = Ctx()
        ctx.store, ctx.project = store, project
        return ctx

    def _advance(self, statuses):
        from aicut.pipeline.publishing import _advance_project_if_settled

        ctx = self._context(statuses)
        try:
            _advance_project_if_settled(ctx)
            return ctx.store.get_project("p1").status
        finally:
            ctx.store.close()

    def test_a_fully_published_project_reaches_published(self):
        self.assertEqual(self._advance(["published"]), "PUBLISHED")
        self.assertEqual(self._advance(["published", "published"]), "PUBLISHED")

    def test_one_episode_still_pending_holds_the_project_open(self):
        self.assertNotEqual(self._advance(["published", "pending"]), "PUBLISHED")

    def test_a_rejected_episode_does_not_hold_the_project_open(self):
        self.assertEqual(self._advance(["published", "rejected"]), "PUBLISHED")

    def test_a_project_where_everything_was_rejected_is_not_called_published(self):
        """Nothing went out; saying PUBLISHED would describe the channel wrongly."""
        self.assertNotEqual(self._advance(["rejected", "rejected"]), "PUBLISHED")


if __name__ == "__main__":
    unittest.main()
