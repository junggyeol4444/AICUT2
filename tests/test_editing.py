"""4.3: what the editing of a finished video is, measured from the file."""

import unittest

from aicut.analysis.editing import Cut, EditFingerprint, detect_cuts, fingerprint
from aicut.config import CalibrationProfile
from aicut.media.vision import MotionSample


def samples(scores, interval=1.0):
    return [MotionSample(at_sec=i * interval, score=s) for i, s in enumerate(scores)]


class CutDetectionTests(unittest.TestCase):
    def setUp(self):
        self.profile = CalibrationProfile.load()

    def test_a_quiet_stretch_has_no_cuts(self):
        self.assertEqual(detect_cuts(samples([0.02] * 20), self.profile), [])

    def test_a_single_spike_is_one_cut(self):
        scores = [0.02] * 10
        scores[5] = 0.9
        cuts = detect_cuts(samples(scores), self.profile)
        self.assertEqual([c.at_sec for c in cuts], [5.0])

    def test_a_busy_picture_is_not_a_cut_every_second(self):
        """A bright game sits high all the way through; a fixed bar alone fails."""
        cuts = detect_cuts(samples([0.55] * 30), self.profile)
        self.assertEqual(cuts, [], "a flat-but-high stretch was read as constant cutting")

    def test_a_spike_inside_a_busy_picture_still_reads_as_a_cut(self):
        scores = [0.5] * 20
        scores[12] = 0.99
        cuts = detect_cuts(samples(scores), self.profile)
        self.assertEqual([c.at_sec for c in cuts], [12.0])

    def test_an_empty_curve_is_not_an_error(self):
        self.assertEqual(detect_cuts([], self.profile), [])

    def test_samples_are_read_in_time_order_whatever_order_they_arrive(self):
        scores = [0.02] * 10
        scores[7] = 0.9
        shuffled = list(reversed(samples(scores)))
        self.assertEqual([c.at_sec for c in detect_cuts(shuffled, self.profile)], [7.0])

    def test_the_thresholds_come_from_the_profile(self):
        """17.1: a channel that plays one game has a different baseline."""
        scores = [0.02] * 10
        scores[5] = 0.35
        # with_overrides replaces the block, so the siblings come along.
        strict = self.profile.with_overrides(
            {"editing": {"cut_score_floor": 0.8, "cut_local_ratio": 1.8, "cut_local_window": 3}},
            measured=["editing.cut_score_floor"],
        )
        self.assertEqual(detect_cuts(samples(scores), self.profile) and True, True)
        self.assertEqual(detect_cuts(samples(scores), strict), [])


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.profile = CalibrationProfile.load()

    def _fast_then_slow(self):
        # First half cut every 2s, second half one long take.
        scores = [0.02] * 60
        for at in range(2, 30, 2):
            scores[at] = 0.9
        return fingerprint(samples(scores), 60.0, self.profile)

    def test_shot_lengths_cover_the_whole_video(self):
        print_me = self._fast_then_slow()
        self.assertAlmostEqual(sum(print_me.shot_lengths_sec), 60.0, places=3)

    def test_the_first_shot_starts_at_zero_and_the_last_ends_at_the_duration(self):
        result = fingerprint(samples([0.02] * 10), 10.0, self.profile)
        self.assertEqual(result.shot_lengths_sec, [10.0], "an uncut video is one shot")

    def test_cuts_per_minute_is_reported(self):
        result = self._fast_then_slow()
        self.assertGreater(result.cut_count, 10)
        self.assertAlmostEqual(result.cuts_per_minute, result.cut_count / 1.0, places=2)

    def test_the_long_take_shows_up_as_the_longest_shot(self):
        result = self._fast_then_slow()
        self.assertGreater(result.longest_shot_sec, 20.0)
        self.assertLess(result.median_shot_sec, 5.0)

    def test_pace_over_time_separates_a_fast_opening_from_a_slow_one(self):
        """4.3 asks about 템포 and 정보 공개 순서; one average cannot say it."""
        pace = self._fast_then_slow().pace_over_time(buckets=2)
        self.assertEqual(len(pace), 2)
        self.assertGreater(pace[0], pace[1])

    def test_pace_buckets_of_zero_are_empty_not_a_division_error(self):
        self.assertEqual(self._fast_then_slow().pace_over_time(buckets=0), [])

    def test_a_zero_length_video_reports_zeroes_rather_than_dividing(self):
        empty = EditFingerprint(duration_sec=0.0)
        self.assertEqual(empty.cuts_per_minute, 0.0)
        self.assertEqual(empty.mean_shot_sec, 0.0)
        self.assertEqual(empty.pace_over_time(), [])

    def test_the_dict_carries_what_the_analysis_needs(self):
        payload = self._fast_then_slow().to_dict()
        for key in ("cut_count", "cuts_per_minute", "mean_shot_sec", "median_shot_sec",
                    "longest_shot_sec", "pace_over_time", "cuts_sec"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
