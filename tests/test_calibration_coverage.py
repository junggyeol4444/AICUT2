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
            self.assertIn("environment", json.loads(path.read_text())["_meta"])
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
