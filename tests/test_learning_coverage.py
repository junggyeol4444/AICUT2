"""Every item the spec asks the learning loops for is actually asked for.

The three loops are answered by a model, so what they cover is decided by what
the prompt asks and what the payload carries. A missing item is not a crash —
it is a question nobody ever asks, and it stays missing until someone reads the
clause again. So the clauses are the test.

4.3 / 4.5 / 12.1 in the merged spec; 6.3 / 8장 / 9장 / 26장 in the original.
"""

import unittest

from aicut.intelligence.knowledge import ProductionKnowledge, consolidate
from aicut.llm.prompts import _TASKS


class ReferencePromptTests(unittest.TestCase):
    """4.3 names four groups and every item in them."""

    def setUp(self):
        self.prompt = _TASKS["analyze_reference"]

    def test_the_four_groups_of_4_3_are_named(self):
        for group in ("structure", "editing", "storytelling", "people"):
            self.assertIn(group, self.prompt)

    def test_every_structure_item_is_asked_for(self):
        for item in ("시작 방식", "정보 공개 순서", "사건 진행", "장면 연결",
                     "결말", "종료 방식"):
            self.assertIn(item, self.prompt, f"4.3 영상 구조: {item} is not asked for")

    def test_every_editing_item_is_asked_for(self):
        for item in ("컷", "평균 장면 길이", "확대", "크롭", "화면 전환", "자막",
                     "강조", "효과", "효과음", "BGM", "이미지", "밈", "리플레이"):
            self.assertIn(item, self.prompt, f"4.3 편집: {item} is not asked for")

    def test_every_storytelling_item_is_asked_for(self):
        for item in ("어떤 정보를 먼저 보여주는가", "어떤 정보를 늦게 공개하는가",
                     "어떤 장면을 생략하는가", "어떤 장면을 반복하는가",
                     "서로 다른 시간대의 장면을 어떻게 연결하는가"):
            self.assertIn(item, self.prompt, f"4.3 스토리텔링: {item} is not asked for")

    def test_the_people_group_is_asked_for(self):
        """The one 4.3 group that was missing entirely."""
        for item in ("누가 중심 인물인가", "누구의 반응이 중요한가",
                     "인물 간 관계가 어떻게 표현되는가"):
            self.assertIn(item, self.prompt, f"4.3 인물: {item} is not asked for")

    def test_4_4_asks_past_the_surface(self):
        self.assertIn("많은 자막, 빠른 컷", self.prompt)
        self.assertIn("production_logic", self.prompt)


class SourceOutputPromptTests(unittest.TestCase):
    """원본 9장 asks nine questions about a pair."""

    def setUp(self):
        self.prompt = _TASKS["compare_source_output"]

    def test_all_nine_questions_are_asked(self):
        for korean, key in (
            ("원본에서 어떤 장면이 선택되었는가", "selected"),
            ("어떤 장면이 제거되었는가", "dropped"),
            ("어떤 장면이 연결되었는가", "joined"),
            ("원본 시간 순서가 어떻게 변경되었는가", "reordered"),
            ("어떤 장면이 반복되었는가", "repeated"),
            ("어떤 장면이 강조되었는가", "emphasised"),
            ("어떤 자막이 추가되었는가", "subtitles"),
            ("어떤 효과가 사용되었는가", "effects"),
            ("어떤 스토리로 재구성되었는가", "retold"),
        ):
            with self.subTest(question=korean):
                self.assertIn(korean, self.prompt)
                self.assertIn(f'"{key}"', self.prompt, f"{key} is not in the return shape")

    def test_the_editing_rule_is_what_12_3_b_is_for(self):
        self.assertIn("inferred_rules", self.prompt)

    def test_code_does_not_pre_label_emphasis(self):
        """18장 gives 편집 의도 to the AI; the payload carries measurements."""
        self.assertIn("what counts as 강조 is your answer", self.prompt)


class PerformancePromptTests(unittest.TestCase):
    def test_the_retention_curve_is_where_12_1_hides_two_metrics(self):
        prompt = _TASKS["learn_from_performance"]
        for item in ("조회수", "클릭률", "평균 시청 지속 시간", "시청자 유지율",
                     "이탈 구간", "재시청 구간", "좋아요", "댓글", "공유"):
            self.assertIn(item, prompt, f"12.1: {item} is not mentioned")


class KnowledgeFieldTests(unittest.TestCase):
    """4.5 lists what the store holds, and 8장 adds two."""

    def test_every_listed_kind_of_pattern_has_a_field(self):
        fields = set(ProductionKnowledge().__dict__)
        for korean, field in (
            ("콘텐츠 구성 패턴", "structure_patterns"),
            ("편집 패턴", "editing_patterns"),
            ("스토리텔링 패턴", "storytelling_patterns"),
            ("장면 선택 패턴", "scene_selection_patterns"),
            ("영상 템포", "pacing_patterns"),
            ("자막 사용 패턴", "subtitle_patterns"),
            ("반응 강조 방식", "emphasis_patterns"),
            ("제목 패턴", "title_patterns"),
            ("썸네일 패턴", "thumbnail_patterns"),
            ("영상 길이와 구성의 관계", "length_structure_patterns"),
            ("콘텐츠별 특징", "content_type_patterns"),
            ("시청자 반응과 영상 구성의 관계", "response_structure_patterns"),
        ):
            with self.subTest(item=korean):
                self.assertIn(field, fields, f"4.5/8장: {korean} has nowhere to go")

    def test_an_analysis_reaches_every_one_of_them(self):
        analysis = {
            "structure": {"시작 방식": "결과부터"},
            "editing": {"컷": "빠름"},
            "storytelling": {"생략": "이동 구간"},
            "scene_selection": {"기준": "반응"},
            "pacing": {"템포": "초반 빠름"},
            "subtitles": {"사용": "많음"},
            "emphasis": {"방식": "확대 + 효과음"},
            "people": {"중심": "호스트"},
            "content_type": {"유형": "합방"},
            "length_and_structure": {"관계": "12분에 3막"},
            "response_and_structure": {"관계": "초반 훅에 댓글 몰림"},
            "production_logic": "결과 먼저, 원인 나중",
            "title_pattern": "질문형",
            "thumbnail_pattern": "표정 클로즈업",
        }
        knowledge = consolidate([analysis, analysis])
        self.assertEqual(knowledge.sample_size, 2)
        for name, value in knowledge.__dict__.items():
            if name in ("sample_size", "performance_learning", "source_output_rules"):
                continue
            with self.subTest(field=name):
                self.assertTrue(value, f"{name} came out empty")

    def test_the_planner_is_shown_all_of_it(self):
        summary = ProductionKnowledge(sample_size=1).summary_for_planner()
        for key in ("pacing", "emphasis", "people", "content_type",
                    "length_and_structure", "response_and_structure", "production_logic"):
            self.assertIn(key, summary)

    def test_knowledge_survives_a_round_trip_with_the_new_fields(self):
        knowledge = ProductionKnowledge(pacing_patterns=[{"pattern": "fast open", "support": 3}])
        restored = ProductionKnowledge.from_dict(knowledge.to_dict())
        self.assertEqual(restored.pacing_patterns, knowledge.pacing_patterns)


if __name__ == "__main__":
    unittest.main()


class Mvp1GateTests(unittest.TestCase):
    """19장 MVP 1: 사람이 봤을 때 분석 결과가 실제 영상의 제작 의도와
    일치한다고 판단되는 비율. Nothing collected that judgement before."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from aicut.db.store import Store

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(str(Path(self._tmp.name) / "aicut.db"))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_an_unjudged_analysis_leaves_the_rate_unmeasured(self):
        """None is not zero. A gate nobody has judged has not failed."""
        from aicut.intelligence import reference

        self.store.save_reference("v1", "c1", {}, {"why": "…"})
        rate = reference.reference_agreement(self.store)

        self.assertEqual((rate["analysed"], rate["judged"]), (1, 0))
        self.assertIsNone(rate["agreement"])

    def test_the_rate_counts_only_what_a_person_judged(self):
        from aicut.intelligence import reference

        first = self.store.save_reference("v1", "c1", {}, {"why": "a"})
        second = self.store.save_reference("v2", "c1", {}, {"why": "b"})
        self.store.save_reference("v3", "c1", {}, {"why": "c"})

        reference.record_reference_verdict(self.store, first, "agree")
        reference.record_reference_verdict(self.store, second, "disagree", "제작 의도가 아님")

        rate = reference.reference_agreement(self.store)
        self.assertEqual((rate["analysed"], rate["judged"], rate["agreed"]), (3, 2, 1))
        self.assertEqual(rate["agreement"], 0.5)

    def test_the_note_survives_with_the_verdict(self):
        from aicut.intelligence import reference

        ref_id = self.store.save_reference("v1", "c1", {}, {"why": "a"})
        reference.record_reference_verdict(self.store, ref_id, "disagree", "썸네일 얘기가 아님")

        stored = self.store.references()[0]["human_verdict"]
        self.assertTrue(stored.startswith("disagree"))
        self.assertIn("썸네일 얘기가 아님", stored)

    def test_an_unknown_reference_or_verdict_is_refused(self):
        from aicut.intelligence import reference

        ref_id = self.store.save_reference("v1", "c1", {}, {})
        with self.assertRaises(KeyError):
            reference.record_reference_verdict(self.store, "nope", "agree")
        with self.assertRaises(ValueError):
            reference.record_reference_verdict(self.store, ref_id, "maybe")

    def test_an_older_database_gains_the_verdict_column(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from aicut.db.store import Store

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "old.db"
            Store(str(path)).close()
            conn = sqlite3.connect(path)
            conn.execute("ALTER TABLE tb_yt_reference DROP COLUMN human_verdict")
            conn.commit()
            conn.close()

            reopened = Store(str(path))
            try:
                columns = {r["name"] for r in
                           reopened.conn.execute("PRAGMA table_info(tb_yt_reference)")}
            finally:
                reopened.close()
            self.assertIn("human_verdict", columns)
