"""6장: what a content candidate is, and what decides its boundary.

6.1 says a candidate is not a clip and lists the ten things it holds. 6.2 says
the boundary is the event, never the screen. 6.3 says most candidates are not
made. 6.4 says the signals are hints. Each is checked against the code that has
to honour it.
"""

import unittest

from aicut.db.store import Store
from aicut.llm.prompts import _TASKS
from aicut.models import ContentCandidate, Project


class CandidateShapeTests(unittest.TestCase):
    """6.1 / 원본 16장: 각 후보는 다음과 같은 정보를 가진다."""

    ITEMS = (
        ("핵심 내용", "core_summary"),
        ("관련 인물", "people"),
        ("관련 사건", "related_event_ids"),
        ("관련 장면", "scenes"),
        ("시작 지점", "start_point"),
        ("주요 변화", "key_changes"),
        ("결과", "outcome"),
        ("필요한 맥락", "required_context"),
        ("다른 사건과의 관계", "event_relations"),
        ("독립 콘텐츠로서의 가능성", "independence_score"),
    )

    def test_every_one_of_the_ten_has_a_field(self):
        fields = set(ContentCandidate().__dict__)
        for korean, field in self.ITEMS:
            with self.subTest(item=korean):
                self.assertIn(field, fields, f"6.1: {korean} has nowhere to go")

    def test_every_one_is_asked_for(self):
        prompt = _TASKS["discover_candidates"]
        for korean, field in self.ITEMS:
            with self.subTest(item=korean):
                self.assertIn(korean, prompt, f"6.1: {korean} is not asked for")
                self.assertIn(field, prompt)

    def test_a_resolution_is_content_not_only_a_flag(self):
        """"결과" is what happened, which a boolean cannot carry."""
        self.assertIsInstance(ContentCandidate().outcome, str)

    def test_the_form_is_the_ai_s_own_word_not_an_enum(self):
        """2.3 forbids promoting a label to a fixed output category."""
        candidate = ContentCandidate(suggested_form="아무 말이나")
        self.assertEqual(candidate.suggested_form, "아무 말이나")

    def test_all_ten_survive_the_database(self):
        candidate = ContentCandidate(
            candidate_id="c1", project_id="p1", core_summary="보스 격파",
            people=["호스트", "게스트"],
            related_event_ids=["e1"],
            scenes=[{"start_sec": 100.0, "end_sec": 160.0, "what": "첫 시도"}],
            start_point="첫 시도 직전", start_sec=95.0,
            key_changes=["패배에서 승리로"], outcome="결국 잡음",
            required_context="이 보스를 3시간 잡고 있었다는 것",
            event_relations=[{"event_id": "e2", "how": "원인"}],
            suggested_form="long", independence_score=0.8, density_score=0.7,
        )
        store = Store(":memory:")
        try:
            store.create_project(Project(project_id="p1", file_path="/x/b.mp4", duration_sec=3600.0))
            store.replace_candidates("p1", [candidate])
            back = store.candidates("p1")[0]
        finally:
            store.close()
        for _, field in self.ITEMS:
            with self.subTest(field=field):
                self.assertEqual(getattr(back, field), getattr(candidate, field))
        self.assertEqual(back.suggested_form, "long")
        self.assertEqual(back.start_sec, 95.0)

    def test_a_workspace_from_before_these_columns_still_opens(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.execute(
                "CREATE TABLE tb_content_candidate (candidate_id TEXT PRIMARY KEY,"
                " project_id TEXT, core_summary TEXT, related_event_ids TEXT,"
                " required_context TEXT, required_context_sec REAL, independence_score REAL,"
                " density_score REAL, has_resolution INTEGER, decision TEXT,"
                " decision_reason TEXT, combine_with TEXT, human_verdict TEXT)"
            )
            old.commit()
            old.close()
            store = Store(path)
            try:
                columns = {r["name"] for r in
                           store.conn.execute("PRAGMA table_info(tb_content_candidate)")}
            finally:
                store.close()
        for _, field in self.ITEMS:
            self.assertIn(field, columns)


class SplitRuleTests(unittest.TestCase):
    """6.2: 분할은 화면 상황이 아니라 사건을 기준으로 한다."""

    def setUp(self):
        self.prompt = _TASKS["discover_candidates"]

    def test_both_directions_of_the_rule_are_stated(self):
        self.assertIn("화면이 섞여도 사건이 하나면", self.prompt)
        self.assertIn("화면이 같아도 사건이 다르면", self.prompt)

    def test_the_two_examples_are_given(self):
        """A rule without its examples is read as "prefer one screen"."""
        self.assertIn("게임 종료 후 토크에서 계속 언급", self.prompt)
        self.assertIn("3시간 연속", self.prompt)

    def test_the_failure_1_2_names_is_named(self):
        self.assertIn("짜깁기", self.prompt)

    def test_the_count_is_not_fixed_and_zero_is_allowed(self):
        self.assertIn("Zero is a correct answer", self.prompt)


class HintTests(unittest.TestCase):
    """6.4: 이 신호만으로 콘텐츠를 확정하지 않는다."""

    def test_hints_are_marked_as_hints_in_the_prompt(self):
        self.assertIn("hints only", _TASKS["discover_candidates"])

    def test_all_three_hint_families_exist(self):
        from aicut.analysis.signals import boundary_hints
        import inspect

        source = inspect.getsource(boundary_hints)
        for kind in ("situation_hold", "tension_floor", "topic_shift"):
            self.assertIn(kind, source, f"6.4: the {kind} hint is missing")


class ValueJudgementTests(unittest.TestCase):
    """6.3 / 원본 18장."""

    def setUp(self):
        self.prompt = _TASKS["evaluate_candidates"]

    def test_the_four_verdicts_of_6_3_are_spelled_out(self):
        for line in ("독립적으로 이해 가능", "재미는 있으나 결말 없음",
                     "맥락이 과도하게 필요", "사건은 있으나 밀도 부족"):
            self.assertIn(line, self.prompt, f"6.3: {line} is not described")

    def test_rejecting_everything_is_allowed(self):
        self.assertIn("rejecting everything", self.prompt)

    def test_the_shorts_case_from_the_original_is_a_produce_not_a_reject(self):
        self.assertIn("짧지만 강한 장면", self.prompt)
        self.assertIn("not a rejection", self.prompt)


class HandOffTests(unittest.TestCase):
    """The ten items are useless if they stop at the database."""

    def _sources(self):
        import inspect
        from aicut.pipeline import evaluating, planning, review

        return {
            "6.3 evaluation": inspect.getsource(evaluating),
            "7장 planning": inspect.getsource(planning.plan_episode),
            "15.4 review screen": inspect.getsource(review.candidate_review),
        }

    def test_each_stage_downstream_is_given_the_whole_candidate(self):
        for where, source in self._sources().items():
            for field in ("people", "scenes", "start_point", "key_changes",
                          "outcome", "event_relations"):
                with self.subTest(where=where, field=field):
                    self.assertIn(field, source, f"{where} never sees 6.1's {field}")


if __name__ == "__main__":
    unittest.main()
