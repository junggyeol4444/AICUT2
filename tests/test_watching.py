"""The analyses look at the videos (4.2, 5.2, 12.3 B), and the media is dropped (4.6).

4.2 lists 영상 first among what loop A collects, and every item 4.3 asks about —
컷 / 평균 장면 길이 / 화면 전환 / 자막 / 강조 / 효과 — is on the screen. 1.2 names
depending on speech alone as the third failure of the tools this replaces, and 5.2
says the passes do not separate 화면 from 소리.

18장 puts the line: the program decodes and samples, and the AI says what the
editing is. Nothing here counts a cut.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aicut.db.store import Store
from aicut.intelligence import reference as reference_mod
from aicut.intelligence.source_output import align_by_transcript, learn
from aicut.llm.mock import MockProducer
from aicut.models import Utterance


def touch(directory: Path, *names: str) -> list[str]:
    made = []
    for name in names:
        path = directory / name
        path.write_bytes(b"not really a jpeg")
        made.append(str(path))
    return made


class ReferenceWatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.producer = MockProducer()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _reference(self):
        return [{"video_id": "abc", "channel_id": "chan", "title": "합방 하이라이트",
                 "description": "", "tags": [], "public_metrics": {"views": 10}}]

    def test_the_frames_are_shown_to_the_analysis(self):
        frames = touch(self.dir, "ref_000.jpg", "ref_001.jpg")
        reference_mod.analyze(
            self.producer, self.store, self._reference(),
            watched={"abc": {"frames": list(frames), "duration_sec": 600.0}},
        )
        self.assertEqual(self.producer.seen_images.get("analyze_reference"), frames,
                         "loop A analysed a video it never looked at")

    def test_the_media_is_kept(self):
        """4.6 leaves the media policy to the operator, and they decided: keep it."""
        frames = touch(self.dir, "ref_000.jpg")
        reference_mod.analyze(
            self.producer, self.store, self._reference(),
            watched={"abc": {"frames": list(frames), "duration_sec": 600.0}},
        )
        self.assertTrue(Path(frames[0]).exists(), "a reference frame was deleted")

    def test_a_reference_with_no_file_still_analyses_from_metadata(self):
        analyses = reference_mod.analyze(self.producer, self.store, self._reference())
        self.assertEqual(len(analyses), 1)
        self.assertFalse(self.producer.seen_images.get("analyze_reference"))

    def test_nothing_in_the_module_counts_cuts(self):
        """18장: 편집 의도 is the AI's. Code that scores scenes took it back."""
        source = Path(reference_mod.__file__).read_text()
        for banned in ("cut_count", "detect_cuts", "fingerprint"):  # noqa: E501
            self.assertNotIn(banned, source, f"{banned} is code deciding the edit")


class PairWatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.producer = MockProducer()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _alignment(self):
        source = [Utterance(0, 10, "보스한테 계속 죽네"), Utterance(500, 510, "드디어 잡았다")]
        output = [Utterance(0, 10, "드디어 잡았다")]
        return align_by_transcript(source, output, source_duration_sec=600.0)

    def test_both_videos_reach_the_analysis_source_first(self):
        """5.2: 화면과 소리를 분리하지 않고 같이 본다."""
        source_frames = touch(self.dir, "s0.jpg", "s1.jpg")
        output_frames = touch(self.dir, "o0.jpg")
        learn(self.producer, self.store, self._alignment(),
              source_ref="s", output_ref="o",
              source_frames=source_frames, output_frames=output_frames)
        self.assertEqual(self.producer.seen_images.get("compare_source_output"),
                         source_frames + output_frames)

    def test_the_payload_says_which_frames_are_which(self):
        source_frames = touch(self.dir, "s0.jpg", "s1.jpg")
        output_frames = touch(self.dir, "o0.jpg")
        learn(self.producer, self.store, self._alignment(),
              source_ref="s", output_ref="o",
              source_frames=source_frames, output_frames=output_frames)
        sent = self.producer.seen_payloads["compare_source_output"]
        self.assertEqual(sent["frames"]["source"], 2)
        self.assertEqual(sent["frames"]["output"], 1)

    def test_a_pair_given_no_files_still_runs_on_the_transcripts(self):
        analysis = learn(self.producer, self.store, self._alignment(),
                         source_ref="s", output_ref="o")
        self.assertIn("measured", analysis)
        self.assertFalse(self.producer.seen_images.get("compare_source_output"))

    def test_code_does_not_label_what_was_emphasised(self):
        """12.3 B asks the analysis what was 강조. A margin in the source is not that."""
        analysis = learn(self.producer, self.store, self._alignment(),
                         source_ref="s", output_ref="o")
        self.assertNotIn("emphasised_spans", analysis["measured"])
        sent = self.producer.seen_payloads["compare_source_output"]
        self.assertNotIn("emphasis", sent["kept"][0])
        self.assertIn("compression", sent["kept"][0])
        self.assertIn("repeated", sent["kept"][0])


if __name__ == "__main__":
    unittest.main()
