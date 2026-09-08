"""11장 / 원본 24·25장: what goes on the video, and who is allowed to decide it.

11.1 names three thumbnail signals. 11.2 names four pieces of metadata and says
the reference patterns are a reference, not a template. The original 24장 adds
업로드 정보. 11.3 keeps one decision away from all of it.
"""

import json
import unittest

from aicut.config import CalibrationProfile
from aicut.analysis.tension import TensionCurve
from aicut.llm.prompts import _TASKS
from aicut.media.faces import FaceReading
from aicut.media.vision import MotionSample
from aicut.render.thumbnails import score_frames


class ThumbnailSignalTests(unittest.TestCase):
    """11.1: 오디오 텐션, 표정 변화 폭, 화면 사건 발생 여부."""

    def setUp(self):
        self.profile = CalibrationProfile.load()
        self.tension = TensionCurve(times=[0.0, 1.0, 2.0], values=[0.2, 0.9, 0.2])
        self.motion = [MotionSample(at_sec=float(i), score=0.1) for i in range(3)]

    def _faces(self):
        return [
            FaceReading(at_sec=0.0, face_ratio=0.10, box=(100, 100, 200, 200)),
            FaceReading(at_sec=1.0, face_ratio=0.35, box=(500, 400, 620, 560)),
            FaceReading(at_sec=2.0, face_ratio=0.35, box=(500, 400, 620, 560)),
        ]

    def test_the_expression_signal_comes_from_faces_not_from_the_frame_delta(self):
        picked = score_frames(3.0, self.tension, self.motion, self.profile, faces=self._faces())
        moment = next(c for c in picked if c.at_sec == 1.0)
        self.assertIn("expression_change", moment.signals)
        self.assertNotEqual(
            moment.signals["expression_change"], moment.signals["screen_event"],
            "표정 변화 폭 and 화면 사건 are the same number again",
        )

    def test_without_a_detector_the_signal_is_absent_not_faked(self):
        """A weight applied to a stand-in is a weight 17장 cannot calibrate."""
        picked = score_frames(3.0, self.tension, self.motion, self.profile)
        self.assertNotIn("expression_change", picked[0].signals)
        self.assertIn("screen_event", picked[0].signals)

    def test_a_moving_face_lifts_a_frame_over_an_identical_still_one(self):
        # Far enough apart to clear thumbnail.min_gap_sec, so both are picked.
        tension = TensionCurve(times=[float(i) for i in range(21)], values=[0.5] * 21)
        motion = [MotionSample(at_sec=float(i), score=0.1) for i in range(21)]
        faces = [
            FaceReading(at_sec=1.0, face_ratio=0.10, box=(100, 100, 200, 200)),
            FaceReading(at_sec=2.0, face_ratio=0.40, box=(600, 500, 760, 700)),
            FaceReading(at_sec=14.0, face_ratio=0.25, box=(300, 300, 400, 400)),
            FaceReading(at_sec=15.0, face_ratio=0.25, box=(300, 300, 400, 400)),
        ]
        picked = {c.at_sec: c for c in
                  score_frames(21.0, tension, motion, self.profile, faces=faces)}
        # The reaction is the top pick; the still stretch is only a filler one.
        # Both are here, and only the audio and screen signals are equal.
        self.assertIn(0.0, picked, "the reacting moment was not a candidate")
        self.assertIn(15.0, picked)
        self.assertGreater(picked[0.0].signals["expression_change"],
                           picked[15.0].signals["expression_change"])
        self.assertGreater(picked[0.0].score, picked[15.0].score)

    def test_the_window_is_a_profile_value(self):
        self.assertIsInstance(self.profile.get_float("thumbnail.expression_window_sec"), float)

    def test_all_three_weights_still_exist(self):
        weights = self.profile.get("thumbnail.weights")
        for signal in ("audio_tension", "expression_change", "screen_event"):
            self.assertIn(signal, weights, f"11.1: no weight for {signal}")


class PackagePromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = _TASKS["package_metadata"]

    def test_the_four_pieces_of_11_2(self):
        for piece in ("제목 후보 3종", "설명(타임스탬프 포함)", "태그", "챕터"):
            self.assertIn(piece, self.prompt, f"11.2: {piece} is not asked for")

    def test_the_reference_patterns_are_a_reference_not_a_template(self):
        self.assertIn("고정 템플릿을 쓰지 않는다", self.prompt)
        self.assertIn("현재 영상 내용에 맞춰 새로 생성한다", self.prompt)

    def test_upload_information_is_part_of_the_package(self):
        """원본 24장 lists 업로드 정보 with the title and the thumbnail."""
        self.assertIn("업로드 정보", self.prompt)
        self.assertIn("category_id", self.prompt)
        self.assertIn("language", self.prompt)

    def test_privacy_is_not_the_model_s_to_set(self):
        """11.3: 검수 게이트를 통과하지 않은 영상은 공개되지 않는다."""
        self.assertIn("Privacy is not yours to set", self.prompt)
        self.assertNotIn('"privacy"', self.prompt)


class UploadInfoTests(unittest.TestCase):
    def test_the_client_has_no_hardcoded_category(self):
        """2.3: a category baked into code is a fixed output type viewers see."""
        import inspect

        from aicut.intelligence.youtube import YouTubeClient

        signature = inspect.signature(YouTubeClient.upload)
        self.assertIsNone(signature.parameters["category_id"].default)

    def test_the_package_carries_only_the_three_upload_keys(self):
        from aicut.llm.mock import MockProducer

        answer = MockProducer().package_metadata({"core_summary": "보스 격파"})
        self.assertEqual(set(answer["upload"]), {"category_id", "language", "playlist"})
        self.assertNotIn("privacy", answer["upload"])

    def test_the_profile_holds_the_fallbacks(self):
        profile = CalibrationProfile.load()
        self.assertEqual(profile.get("upload.default_category_id"), "20")
        self.assertTrue(profile.get("upload.default_language"))

    def test_privacy_on_upload_is_still_the_profile_s(self):
        """The one upload decision 11.3 puts outside the model entirely."""
        self.assertEqual(CalibrationProfile.load().get("upload.privacy_on_upload"), "private")


if __name__ == "__main__":
    unittest.main()


class PackageIsCheckedAgainstYouTubeTests(unittest.TestCase):
    """11.2 asks for 챕터 and 설명(타임스탬프 포함).

    YouTube reads chapters all-or-nothing: unless the first is 0:00, there are
    at least three, and each runs at least 10 seconds, it ignores every one of
    them and the video ships with no chapters. A list that gets thrown away is
    not the 챕터 11.2 asked for, and nothing said so before the upload.
    """

    def _episode(self, **metadata):
        from aicut.models import Episode

        episode = Episode(episode_id="e1")
        episode.metadata = {
            "titles": ["a", "b", "c"],
            "description": "0:00 시작\n1:30 사건",
            "chapters": [{"at_sec": 0.0, "label": "시작"},
                         {"at_sec": 60.0, "label": "사건"},
                         {"at_sec": 120.0, "label": "결과"}],
            **metadata,
        }
        return episode

    def test_a_package_youtube_will_accept_reports_nothing(self):
        from aicut.pipeline.packaging import check_package

        self.assertEqual(
            check_package(self._episode(), [0.0, 60.0, 120.0], 180.0), [],
        )

    def test_a_list_that_does_not_start_at_zero_is_reported(self):
        from aicut.pipeline.packaging import check_package

        episode = self._episode(chapters=[{"at_sec": 12.0, "label": "a"},
                                          {"at_sec": 60.0, "label": "b"},
                                          {"at_sec": 120.0, "label": "c"}])
        problems = check_package(episode, [12.0, 60.0, 120.0], 180.0)

        self.assertTrue(any("0:00" in p for p in problems), problems)

    def test_too_few_chapters_and_a_chapter_that_is_too_short(self):
        from aicut.pipeline.packaging import check_package

        two = self._episode(chapters=[{"at_sec": 0.0, "label": "a"},
                                      {"at_sec": 60.0, "label": "b"}])
        self.assertTrue(any("at least 3" in p for p in check_package(two, [0.0, 60.0], 180.0)))

        brief = self._episode(chapters=[{"at_sec": 0.0, "label": "a"},
                                        {"at_sec": 4.0, "label": "b"},
                                        {"at_sec": 60.0, "label": "c"}])
        self.assertTrue(any("4.0s" in p for p in check_package(brief, [0.0, 4.0, 60.0], 180.0)))

    def test_a_chapter_past_the_end_is_reported(self):
        from aicut.pipeline.packaging import check_package

        episode = self._episode(chapters=[{"at_sec": 0.0, "label": "a"},
                                          {"at_sec": 60.0, "label": "b"},
                                          {"at_sec": 900.0, "label": "c"}])
        problems = check_package(episode, [0.0, 60.0, 900.0], 180.0)

        self.assertTrue(any("past the end" in p for p in problems), problems)

    def test_a_chapter_that_is_not_on_a_cut_boundary_is_reported(self):
        """The payload names where a chapter may sit; anywhere else is mid-scene."""
        from aicut.pipeline.packaging import check_package

        problems = check_package(self._episode(), [0.0, 60.0, 119.0], 180.0)

        self.assertTrue(any("cut boundary" in p for p in problems), problems)

    def test_a_description_without_a_timestamp_is_reported(self):
        from aicut.pipeline.packaging import check_package

        episode = self._episode(description="오늘 방송에서 있었던 일")
        problems = check_package(episode, [0.0, 60.0, 120.0], 180.0)

        self.assertTrue(any("타임스탬프" in p for p in problems), problems)

    def test_fewer_than_three_titles_is_reported(self):
        from aicut.pipeline.packaging import check_package

        problems = check_package(self._episode(titles=["only one"]), [0.0, 60.0, 120.0], 180.0)

        self.assertTrue(any("3종" in p for p in problems), problems)

    def test_the_check_reports_and_never_rewrites(self):
        """11.2 makes the metadata the model's; 18장 keeps it there."""
        from aicut.pipeline.packaging import check_package

        episode = self._episode(chapters=[{"at_sec": 5.0, "label": "a"}])
        before = json.dumps(episode.metadata, sort_keys=True, ensure_ascii=False)
        check_package(episode, [0.0], 180.0)

        self.assertEqual(json.dumps(episode.metadata, sort_keys=True, ensure_ascii=False), before)
