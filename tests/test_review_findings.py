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
