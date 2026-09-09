import json
import tempfile
import unittest
from pathlib import Path

from backend.calibration import CalibrationError, PacingSample, calibrate_pacing, classify
from backend.database import Database


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.samples = json.loads((Path(__file__).parent / "fixtures" / "pacing-samples.json").read_text())

    def test_sweep_derives_profile_from_labeled_samples(self):
        result = calibrate_pacing(self.samples)
        self.assertEqual(result.sample_count, 8)
        self.assertGreaterEqual(result.f1, .8)
        self.assertIn("silence_db_max", result.params)
        self.assertLessEqual(result.params["trim_duration_min_sec"], result.params["cut_duration_min_sec"])

    def test_context_preservation_overrides_mechanical_silence_cut(self):
        params = calibrate_pacing(self.samples).params
        reaction = PacingSample(-60, 10, .01, False, True, "KEEP")
        transition = PacingSample(-60, 10, .01, True, False, "KEEP")
        self.assertEqual(classify(reaction, params), "KEEP")
        self.assertEqual(classify(transition, params), "KEEP")

    def test_rejects_insufficient_or_invalid_labels(self):
        with self.assertRaises(CalibrationError):
            calibrate_pacing(self.samples[:3])
        invalid = {**self.samples[0], "human_decision": "DELETE"}
        with self.assertRaises(CalibrationError):
            PacingSample.from_dict(invalid)

    def test_profile_is_persisted_per_channel(self):
        result = calibrate_pacing(self.samples)
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "calibration.db")
            saved = database.save_calibration("channel-a", "게임", result.to_dict(), result.f1 * 100)
            database.save_calibration("channel-b", "토크", result.to_dict(), result.f1 * 100)
            profiles = database.list_calibrations("channel-a")
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["profile_id"], saved["profile_id"])
        self.assertEqual(profiles[0]["params"]["sample_count"], 8)
