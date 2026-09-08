"""15장: the four screens, and what each one has to show.

15.1 is the flow. 15.2 is what goes in. 15.3 asks for 현재 상태(14장)와 진행률.
15.4 is the screen MVP 3 is scored from. 15.5 is the episode card and the
작업 리포트.

The page is one static file, so these read it as text. Crude, but the failure
they catch is real: a field added to the API that no screen ever shows.
"""

import unittest
from pathlib import Path

from aicut.pipeline.states import PROGRESS_ORDER, State, progress
from aicut.ui.jobs import Job

# encoding is explicit: the page is full of Korean, and Windows defaults
# read_text() to cp1252, which cannot decode it. CI caught it there.
PAGE = (Path(__file__).resolve().parents[1] / "aicut" / "ui" / "static"
        / "index.html").read_text(encoding="utf-8")


class ProgressTests(unittest.TestCase):
    """15.3: 현재 상태(14장)와 진행률."""

    def test_the_walk_only_contains_states_a_run_passes_through(self):
        for state in PROGRESS_ORDER:
            self.assertIsInstance(state, State)
        for terminal in (State.NO_CONTENT, State.FAILED, State.RETRY_QUEUED):
            self.assertNotIn(terminal, PROGRESS_ORDER)

    def test_it_runs_from_zero_to_one(self):
        self.assertEqual(progress(State.QUEUED), 0.0)
        self.assertEqual(progress(State.PUBLISHED), 1.0)

    def test_it_only_ever_moves_forward(self):
        seen = [progress(s) for s in PROGRESS_ORDER]
        self.assertEqual(seen, sorted(seen))

    def test_a_run_that_found_nothing_is_finished_not_sixty_percent(self):
        """NO_CONTENT is a correct ending (1.3), so the bar fills."""
        self.assertEqual(progress(State.NO_CONTENT), 1.0)
        self.assertEqual(progress(State.FAILED), 1.0)

    def test_an_unknown_state_does_not_raise(self):
        self.assertEqual(progress("SOMETHING_ELSE"), 0.0)

    def test_the_job_reports_it(self):
        job = Job(job_id="j", project_id="p", source="/x.mp4", state=State.PLANNING.value)
        self.assertEqual(job.to_dict()["progress"], progress(State.PLANNING))

    def test_the_page_draws_it(self):
        self.assertIn('id="job-bar"', PAGE)
        self.assertIn("job.progress", PAGE)


class CandidateScreenTests(unittest.TestCase):
    """15.4, which is where MVP 3's agreement number comes from."""

    def test_every_6_1_item_is_on_the_screen(self):
        for label in ("관련 인물", "시작 지점", "주요 변화", "결과",
                      "필요한 맥락", "다른 사건과의 관계", "관련 장면"):
            self.assertIn(label, PAGE, f"6.1: {label} is not shown to the reviewer")

    def test_the_decision_and_its_reason_are_shown(self):
        self.assertIn("판단 근거", PAGE)
        self.assertIn("c.decision", PAGE)

    def test_agree_and_disagree_are_both_offered(self):
        self.assertIn('data-v="agree"', PAGE)
        self.assertIn('data-v="disagree"', PAGE)


class ResultsScreenTests(unittest.TestCase):
    """15.5: 썸네일 프리뷰, 제목 후보 3종, 재생 프리뷰, 편집 계획 열람, 업로드 / 폴더 열기."""

    def test_all_six_episode_card_affordances(self):
        for marker in ("data-thumb", "titles", "<video", "data-plan",
                       "data-upload", "data-reveal"):
            self.assertIn(marker, PAGE, f"15.5: {marker} is missing from the episode card")

    def test_the_work_report_names_what_15_5_asks_for(self):
        for label in ("발견 후보", "제작 결정", "제외 사유", "처리 시간"):
            self.assertIn(label, PAGE, f"15.5 작업 리포트: {label} is not shown")

    def test_publishing_is_a_separate_action_from_uploading(self):
        """11.3: uploading is private; going public is the human's own act."""
        self.assertIn("data-publish", PAGE)
        self.assertIn("confirm(", PAGE)

    def test_only_an_approved_episode_offers_the_public_button(self):
        self.assertIn('e.review_status === "approved"', PAGE)

    def test_a_drift_warning_reaches_the_operator(self):
        """17.4 step 4 is useless if the run says it and nothing shows it."""
        self.assertIn("environment_drift", PAGE)


class UploadEndpointTests(unittest.TestCase):
    def test_the_route_exists_and_takes_a_post(self):
        import inspect

        from aicut.ui import server

        source = inspect.getsource(server)
        self.assertIn("/upload$", source)
        self.assertIn("def upload(self, episode_id", source)

    def test_it_cannot_publish_without_going_through_the_gate(self):
        """publish_episode is the one that refuses; this must call it, not set_privacy."""
        import inspect

        from aicut.ui.server import UiServer

        source = inspect.getsource(UiServer.upload)
        self.assertIn("publishing.publish_approved", source)
        self.assertNotIn("set_privacy", source)


class InputPanelTests(unittest.TestCase):
    """15.2."""

    def test_both_container_formats_are_accepted(self):
        self.assertIn(".mp4", PAGE)
        self.assertIn(".mkv", PAGE)

    def test_drag_and_drop_exists(self):
        self.assertIn('id="drop"', PAGE)

    def test_the_length_hint_is_marked_as_a_hint(self):
        self.assertIn('id="hint"', PAGE)

    def test_both_profiles_can_be_picked(self):
        self.assertIn('id="channel"', PAGE)
        self.assertIn('id="profile-pick"', PAGE)


if __name__ == "__main__":
    unittest.main()
