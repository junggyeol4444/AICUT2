"""17장: the thresholds live in the profile, and the setup they were measured in.

17.1 says every 판정 기준 is in the profile, not in code. Three were not, and
one of those decided the ground truth the other two are scored against.

17.4 step 4 asks for a re-measurement when the broadcast setup changes, which
nobody can act on unless something notices.
"""

import unittest

from aicut.calibration import environment as env
from aicut.calibration.metrics import (
    ContentDiscoveryScore, PacingScore, combined_score, score_content_discovery,
)
from aicut.config import CalibrationProfile
from aicut.models import SituationLabel, Utterance


class ThresholdsInTheProfileTests(unittest.TestCase):
    """17.1: 모든 판정 기준을 코드가 아닌 설정 파일에 둔다."""

    def setUp(self):
        self.profile = CalibrationProfile.load()

    def test_the_three_calibration_judgements_are_profile_values(self):
        for key in ("calibration.silence_survival_ratio",
                    "calibration.content_match_min_iou"):
            self.assertIsInstance(self.profile.get_float(key), float)
        weights = self.profile.get("calibration.score_weights")
        self.assertEqual(set(weights), {"discovery", "pacing"})

    def test_they_are_marked_provisional(self):
        """17.5: nothing measured, so nothing written down as measured."""
        self.assertIn("calibration", self.profile.provisional)

    def test_the_match_threshold_changes_what_counts_as_agreement(self):
        system = [(0.0, 100.0)]
        human = [(0.0, 40.0)]           # iou 0.4
        loose = score_content_discovery(system, human, min_iou=0.3)
        strict = score_content_discovery(system, human, min_iou=0.9)
        self.assertEqual(loose.matched, 1)
        self.assertEqual(strict.matched, 0)

    def test_the_profile_supplies_it_when_no_override_is_given(self):
        tight = self.profile.with_overrides(
            {"calibration": {"silence_survival_ratio": 0.6,
                             "content_match_min_iou": 0.95,
                             "score_weights": {"discovery": 0.6, "pacing": 0.4}}},
            measured=["calibration.content_match_min_iou"],
        )
        scored = score_content_discovery([(0.0, 100.0)], [(0.0, 40.0)], profile=tight)
        self.assertEqual(scored.matched, 0)

    def test_the_score_weights_decide_which_profile_a_sweep_picks(self):
        pacing = PacingScore(keep_recall=1.0, cut_precision=1.0, accuracy=1.0,
                             kept_by_human=1, kept_by_system=1, total=2)
        discovery = ContentDiscoveryScore(recall=0.0, precision=0.0, false_positive_rate=1.0,
                                          matched=0, system_count=1, human_count=1)
        pacing_first = self.profile.with_overrides(
            {"calibration": {"silence_survival_ratio": 0.6, "content_match_min_iou": 0.3,
                             "score_weights": {"discovery": 0.1, "pacing": 0.9}}},
            measured=["calibration.score_weights"],
        )
        self.assertGreater(
            combined_score(pacing, discovery, profile=pacing_first),
            combined_score(pacing, discovery, profile=self.profile),
        )

    def test_the_survival_ratio_decides_the_ground_truth_itself(self):
        """It labels what the human did, which 17.3 then scores the system on."""
        import inspect

        from aicut.calibration.dataset import Dataset

        signature = inspect.signature(Dataset.derive_silence_verdicts)
        self.assertIsNone(signature.parameters["survival_ratio"].default,
                          "a constant here decides whether every measurement passes")
        self.assertIn("profile", signature.parameters)


class Span:
    def __init__(self, start, end, label):
        self.start_sec, self.end_sec, self.label = start, end, label


class Media:
    def __init__(self, tracks):
        self.audio_tracks = tracks

    @property
    def is_multitrack(self):
        return len(self.audio_tracks) > 1


class Track:
    def __init__(self, role):
        self.role = role


class EnvironmentTests(unittest.TestCase):
    """17.4 step 4: 방송 환경 변경(마이크·게임·합방 여부) 시 재측정."""

    def _print(self, tracks, gameplay, multi):
        situations = [
            Span(0, gameplay * 100, SituationLabel.GAMEPLAY),
            Span(gameplay * 100, (gameplay + multi) * 100, SituationLabel.MULTI_PERSON),
            Span((gameplay + multi) * 100, 100, SituationLabel.SOLO_TALK),
        ]
        return env.fingerprint(
            Media([Track(r) for r in tracks]), situations,
            [Utterance(0, 1, "hi", speaker="host"), Utterance(2, 3, "yo", speaker="guest")],
        )

    def test_the_three_things_17_4_names_are_all_measured(self):
        got = self._print(["mic", "game"], 0.5, 0.2)
        self.assertEqual(got["mic"]["track_count"], 2)
        self.assertAlmostEqual(got["game"]["gameplay_share"], 0.5, places=3)
        self.assertAlmostEqual(got["collab"]["multi_person_share"], 0.2, places=3)
        self.assertEqual(got["collab"]["speaker_count"], 2)

    def test_a_changed_mic_setup_is_reported(self):
        drift = env.compare(self._print(["mic", "game", "call", "bgm"], 0.5, 0.2),
                            self._print(["mic"], 0.5, 0.2))
        self.assertTrue(any("마이크" in d for d in drift))

    def test_a_channel_that_stopped_playing_games_is_reported(self):
        drift = env.compare(self._print(["mic"], 0.9, 0.0), self._print(["mic"], 0.1, 0.0))
        self.assertTrue(any("게임" in d for d in drift))

    def test_a_solo_channel_that_started_collabing_is_reported(self):
        drift = env.compare(self._print(["mic"], 0.5, 0.0), self._print(["mic"], 0.5, 0.5))
        self.assertTrue(any("합방" in d for d in drift))

    def test_the_same_setup_reports_nothing(self):
        same = self._print(["mic", "game"], 0.6, 0.1)
        self.assertEqual(env.compare(same, same), [])

    def test_normal_variation_between_broadcasts_is_not_drift(self):
        drift = env.compare(self._print(["mic"], 0.60, 0.1), self._print(["mic"], 0.70, 0.1))
        self.assertEqual(drift, [])

    def test_a_profile_with_no_recorded_environment_is_not_a_mismatch(self):
        """Saying it every run for every old profile would be noise."""
        self.assertEqual(env.compare(None, self._print(["mic"], 0.5, 0.0)), [])
        self.assertEqual(env.compare({}, self._print(["mic"], 0.5, 0.0)), [])

    def test_nothing_here_changes_a_parameter(self):
        """17.4 asks for a re-measurement, which needs the 17.2 dataset."""
        import inspect

        source = inspect.getsource(env)
        self.assertNotIn("with_overrides", source)
        self.assertNotIn("set(", source)


class ProfileEnvironmentTests(unittest.TestCase):
    def test_the_environment_round_trips_through_a_saved_profile(self):
        import json
        import tempfile
        from pathlib import Path

        profile = CalibrationProfile.load()
        profile.environment = {"mic": {"track_count": 4, "roles": ["game", "mic"]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            profile.save(path)
            self.assertIn("environment", json.loads(path.read_text(encoding="utf-8"))["_meta"])
            self.assertEqual(CalibrationProfile.load(path).environment, profile.environment)

    def test_an_override_keeps_the_measured_environment(self):
        profile = CalibrationProfile.load()
        profile.environment = {"mic": {"track_count": 4}}
        swept = profile.with_overrides(
            {"calibration": {"silence_survival_ratio": 0.7, "content_match_min_iou": 0.3,
                             "score_weights": {"discovery": 0.6, "pacing": 0.4}}},
            measured=["calibration.silence_survival_ratio"],
        )
        self.assertEqual(swept.environment, {"mic": {"track_count": 4}})


if __name__ == "__main__":
    unittest.main()


class UnreachedThresholdTests(unittest.TestCase):
    """A threshold nobody measured can gate a label out of existence, silently.

    Measured on a real 7-minute broadcast: the face ratio peaked at 0.103 while
    `situation.face_ratio_solo_talk` sits at 0.12, so 단독 토크 was impossible
    for the whole run and the report said 100% 게임 플레이 without comment. The
    run now says the number was never reached.
    """

    class _Signals:
        def __init__(self, faces=(), motion=()):
            self.faces, self.motion = list(faces), list(motion)

    class _Ctx:
        def __init__(self, profile, signals):
            self.profile, self.signals, self.report = profile, signals, {}

        def note(self, key, value):
            self.report[key] = value

    def _run(self, *, peak_face=None, peak_motion=None, labels=()):
        from aicut.media.faces import FaceReading
        from aicut.media.vision import MotionSample
        from aicut.pipeline.understanding import _note_unreached_thresholds

        faces = [FaceReading(at_sec=0.0, face_ratio=peak_face, box=(0, 0, 10, 10))] \
            if peak_face is not None else []
        motion = [MotionSample(at_sec=0.0, score=peak_motion)] if peak_motion is not None else []
        ctx = self._Ctx(CalibrationProfile.load(), self._Signals(faces, motion))
        spans = [type("S", (), {"label": label})() for label in labels]
        _note_unreached_thresholds(ctx, spans)
        return ctx.report.get("threshold_never_reached", [])

    def test_a_face_that_never_fills_enough_of_the_frame_is_reported(self):
        found = self._run(peak_face=0.103)
        names = [f["parameter"] for f in found]
        self.assertIn("situation.face_ratio_solo_talk", names)
        entry = next(f for f in found if f["parameter"] == "situation.face_ratio_solo_talk")
        self.assertEqual(entry["highest_measured"], 0.103)
        self.assertTrue(entry["provisional"], "the value is a guess and should say so")

    def test_a_face_that_does_cross_the_line_is_not_reported(self):
        found = self._run(peak_face=0.5, labels=[SituationLabel.SOLO_TALK])
        self.assertNotIn("situation.face_ratio_solo_talk", [f["parameter"] for f in found])

    def test_nothing_is_claimed_when_nothing_was_measured(self):
        """No detector is not a threshold problem; 5.3 already leaves it UNKNOWN."""
        self.assertEqual(self._run(), [])

    def test_a_source_that_never_moves_is_reported_too(self):
        found = self._run(peak_motion=0.001)
        self.assertIn("situation.away_max_motion", [f["parameter"] for f in found])

    def test_the_threshold_is_reported_not_adjusted(self):
        """17.4 settles a value by measurement; one broadcast is not that."""
        import inspect

        from aicut.pipeline import understanding

        source = inspect.getsource(understanding._note_unreached_thresholds)
        self.assertNotIn("with_overrides", source)
        self.assertIn("does not adjust the value", source)


class Mvp2DensityGateTests(unittest.TestCase):
    """19장 MVP 2 실측 항목: 1차 통과 밀도별 사건 검출률과 처리 시간."""

    def _remembered(self):
        from aicut.calibration.mvp2 import RememberedEvent

        return [
            RememberedEvent(at_sec=100.0, what="싸움이 시작된다"),
            RememberedEvent(at_sec=900.0, what="화해한다"),
        ]

    def _event(self, event_id, start, end, summary="…"):
        class _E:
            def __init__(self):
                self.event_id = event_id
                self.summary = summary

            def span(self):
                return (start, end)

        return _E()

    def test_a_remembered_event_with_no_detected_event_near_it_is_missed(self):
        from aicut.calibration.mvp2 import coverage

        result = coverage(
            [self._event("e1", 60.0, 150.0)], self._remembered(), tolerance_sec=90.0,
        )

        self.assertEqual(len(result.found), 1)
        self.assertEqual(len(result.missed), 1)
        self.assertEqual(result.missed[0]["what"], "화해한다")
        self.assertEqual(result.rate, 0.5)

    def test_tolerance_is_what_decides_a_near_miss(self):
        """A person recalling a six-hour broadcast does not give frame numbers."""
        from aicut.calibration.mvp2 import coverage

        detected = [self._event("e1", 200.0, 260.0)]
        remembered = [self._remembered()[0]]          # at 100s

        self.assertEqual(coverage(detected, remembered, tolerance_sec=30.0).rate, 0.0)
        self.assertEqual(coverage(detected, remembered, tolerance_sec=120.0).rate, 1.0)

    def test_a_perfect_rate_earned_by_one_huge_event_is_visible(self):
        """One event spanning the broadcast matches everything a person wrote
        down while having detected nothing. The rate cannot say that."""
        from aicut.calibration.mvp2 import DensityMeasurement, coverage

        measurement = DensityMeasurement(
            pass1_window_sec=120.0, events=1, seconds=4.0, realtime_factor=250.0,
            coverage=coverage([self._event("e1", 0.0, 1000.0)], self._remembered()),
            duration_sec=1000.0,
        )

        self.assertEqual(measurement.coverage.rate, 1.0)
        self.assertEqual(measurement.as_dict()["widest_match_ratio"], 1.0)

    def test_the_measurement_does_not_destroy_the_project_it_measures(self):
        """`understanding.run` is a production stage: it replaces the project's
        windows and events. Measuring must not cost the analysis."""
        import tempfile
        from pathlib import Path

        from aicut.calibration.mvp2 import measure_densities
        from aicut.config import CalibrationProfile
        from aicut.db.store import Store
        from aicut.llm import get_producer
        from aicut.models import Event, EventMention, Project
        from aicut.pipeline.context import RunContext

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(str(Path(tmp) / "aicut.db"))
            project = store.create_project(Project(
                project_id="p1", file_path="/nowhere.mkv", duration_sec=1200.0,
            ))
            store.replace_events(project.project_id, [Event(
                event_id="kept", project_id=project.project_id, summary="the real one",
                mentions=[EventMention(event_id="kept", source_start_sec=10.0,
                                       source_end_sec=20.0)],
            )])
            ctx = RunContext(project=project, store=store, profile=CalibrationProfile.load(),
                             producer=get_producer("mock"), workspace=Path(tmp))

            def wipe(context):
                context.store.replace_events(context.project.project_id, [])

            rows = measure_densities(ctx, [60.0, 120.0], understand=wipe)

            self.assertEqual([r.events for r in rows], [0, 0])
            surviving = store.events(project.project_id)
            self.assertEqual([e.event_id for e in surviving], ["kept"])
            self.assertIs(ctx.store, store)
            store.close()

    def test_a_remembered_file_without_at_sec_says_what_the_file_should_be(self):
        import json
        import tempfile
        from pathlib import Path

        from aicut.calibration.mvp2 import load_remembered
        from aicut.errors import AicutError

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "remembered.json"
            path.write_text(json.dumps([{"what": "무슨 일"}]), encoding="utf-8")
            with self.assertRaises(AicutError):
                load_remembered(path)

            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaises(AicutError):
                load_remembered(path)

            path.write_text(
                json.dumps([{"at_sec": 90, "what": "b"}, {"at_sec": 10, "what": "a"}]),
                encoding="utf-8",
            )
            self.assertEqual([e.what for e in load_remembered(path)], ["a", "b"])
