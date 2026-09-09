import tempfile
import unittest
from pathlib import Path

from backend.content_intelligence import ReferenceError, validate_reference
from backend.database import Database


class ContentIntelligenceTest(unittest.TestCase):
    def reference(self):
        return {
            "video_id": "youtube-video-one", "channel_ref": "reference-channel",
            "metadata": {"title": "편집 영상", "duration_sec": 600},
            "public_metrics": {"views": 12000, "likes": 800, "comments": 40},
            "media_discarded": True,
            "patterns": [{
                "kind": "STORY", "title": "결과 선공개", "description": "결과 후 원인을 설명한다.",
                "confidence": .82, "evidence": [{"start_sec": 0, "end_sec": 12, "reason": "cold open"}],
            }],
        }

    def test_validates_and_persists_public_reference_patterns(self):
        reference = validate_reference(self.reference())
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "references.db")
            saved = database.save_youtube_reference(reference)
            references = database.list_youtube_references("reference-channel")
            patterns = database.list_production_patterns("STORY")
        self.assertEqual(saved["video_id"], "youtube-video-one")
        self.assertTrue(saved["media_discarded"])
        self.assertEqual(references[0]["public_metrics"]["views"], 12000)
        self.assertEqual(patterns[0]["title"], "결과 선공개")
        self.assertEqual(patterns[0]["evidence"][0]["reason"], "cold open")

    def test_reanalysis_replaces_patterns_without_duplicating_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "references.db")
            first = database.save_youtube_reference(validate_reference(self.reference()))
            changed = self.reference()
            changed["patterns"] = [{
                "kind": "PACING", "title": "반응 보존", "description": "반응 뒤 정적을 보존한다.",
                "confidence": .9, "evidence": [],
            }]
            second = database.save_youtube_reference(validate_reference(changed))
            patterns = database.list_production_patterns()
        self.assertEqual(first["reference_id"], second["reference_id"])
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["kind"], "PACING")

    def test_rejects_private_analytics_and_retained_media_paths_at_any_depth(self):
        for forbidden in (
            {"public_metrics": {"audience_retention": [1, .5]}},
            {"metadata": {"source_media_path": "/downloads/reference.mp4"}},
            {"patterns": [{"kind": "X", "title": "X", "description": "X", "confidence": .5,
                           "evidence": [{"download_path": "/tmp/video.mp4"}]}]},
        ):
            payload = self.reference()
            payload.update(forbidden)
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(ReferenceError, "저장할 수 없는"):
                validate_reference(payload)

    def test_rejects_invalid_metrics_and_pattern_confidence(self):
        payload = self.reference()
        payload["public_metrics"]["views"] = -1
        with self.assertRaises(ReferenceError):
            validate_reference(payload)
        payload = self.reference()
        payload["patterns"][0]["confidence"] = 1.2
        with self.assertRaises(ReferenceError):
            validate_reference(payload)


if __name__ == "__main__":
    unittest.main()
