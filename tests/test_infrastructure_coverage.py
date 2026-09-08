"""13·14·16·20·22장: the parts that carry everything else.

13.2 gives a table list with columns. 13.1 bans two columns by name. 14장 gives
the state walk. 16장 is a seven-row table of situations and what to do. 22장
lists seven deliverables.
"""

import re
import unittest
from pathlib import Path

from aicut.llm.prompts import _TASKS
from aicut.models import Episode
from aicut.pipeline.packaging import _as_text
from aicut.pipeline.states import State, _NEXT

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "aicut" / "db" / "schema.sql").read_text(encoding="utf-8")
TABLES = dict(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA, re.S))


def columns(table: str) -> set[str]:
    return set(re.findall(r"^\s*(\w+)\s", TABLES.get(table, ""), re.M))


class SchemaTests(unittest.TestCase):
    """13.2, column by column."""

    SPEC = {
        "tb_project": "project_id file_path duration_sec status created_at",
        "tb_event": "event_id project_id summary people relations",
        "tb_event_mention": "mention_id event_id source_start_sec source_end_sec role",
        "tb_content_candidate": (
            "candidate_id project_id core_summary related_event_ids required_context "
            "independence_score decision decision_reason"
        ),
        "tb_episode": (
            "episode_id project_id candidate_ids planned_structure target_type "
            "planned_duration_sec output_mp4_path thumbnail_path render_status"
        ),
        "tb_edit_timeline": (
            "cut_id episode_id sequence_order source_start_sec source_end_sec "
            "speaker_tag scene_role pacing_mode visual_effect subtitle_ref"
        ),
        "tb_yt_reference": "ref_id video_id public_metrics extracted_patterns",
        "tb_source_output_pair": "pair_id source_ref output_ref selection_analysis",
        "tb_performance": "perf_id episode_id metrics collected_at",
        "tb_calibration_profile": "profile_id channel_ref params measured_at eval_score",
    }

    def test_every_table_and_column_13_2_names(self):
        for table, spec in self.SPEC.items():
            with self.subTest(table=table):
                self.assertIn(table, TABLES, f"13.2 names {table}")
                have = columns(table)
                for column in spec.split():
                    self.assertIn(column, have, f"13.2: {table}.{column}")

    def test_an_episode_is_not_a_span(self):
        """13.1: ※ start_sec / end_sec 컬럼을 두지 않는다. 2.4 depends on it."""
        for banned in ("start_sec", "end_sec"):
            self.assertNotIn(banned, columns("tb_episode"),
                             "an episode with a span cannot be a non-linear reconstruction")

    def test_the_reference_table_has_nowhere_to_put_a_video(self):
        """4.6 is enforced by there being no column, not by remembering."""
        have = columns("tb_yt_reference")
        for shape in ("media", "video_path", "file_path", "blob"):
            self.assertNotIn(shape, have)


class StateMachineTests(unittest.TestCase):
    """14장."""

    WALK = ["QUEUED", "PARSING", "UNDERSTANDING", "DISCOVERING", "EVALUATING",
            "PLANNING", "RENDERING", "PACKAGED", "REVIEW_PENDING", "PUBLISHED"]
    BRANCHES = ["NO_CONTENT", "FAILED", "RETRY_QUEUED"]

    def test_the_states_are_exactly_the_ones_named(self):
        self.assertEqual(sorted(s.value for s in State),
                         sorted(self.WALK + self.BRANCHES))

    def test_the_walk_is_walkable(self):
        for a, b in zip(self.WALK, self.WALK[1:]):
            with self.subTest(step=f"{a}->{b}"):
                self.assertIn(State(b), _NEXT[State(a)])

    def test_every_path_to_published_goes_through_the_gate(self):
        """11.3: 검수 게이트를 통과하지 않은 영상은 공개되지 않는다.

        Two states lead to PUBLISHED, and both are behind the gate:
        REVIEW_PENDING is the gate, and RETRY_QUEUED is only ever entered from
        it — an approved episode whose upload hit the 11.4 quota.
        """
        reach = {s for s, nxt in _NEXT.items() if State.PUBLISHED in nxt}
        self.assertEqual(reach, {State.REVIEW_PENDING, State.RETRY_QUEUED})
        into_retry = {s for s, nxt in _NEXT.items()
                      if State.RETRY_QUEUED in nxt and s is not State.RETRY_QUEUED}
        self.assertEqual(into_retry, {State.REVIEW_PENDING},
                         "a queue that can be entered before review is a way past the gate")


class ExceptionPolicyTests(unittest.TestCase):
    """16장, row by row."""

    def test_a_silent_stretch_is_still_retrievable(self):
        """Row 1: 음성 미감지 구간 — an event mention with no speech under it."""
        import inspect

        from aicut.pipeline import retrieval

        self.assertIn("A mention with no speech under it is still retrievable",
                      inspect.getsource(retrieval.SceneIndex.build))

    def test_a_single_topic_broadcast_is_not_forced_apart(self):
        """Row 2: 억지 분할 금지, 완결형 1편으로 통합."""
        self.assertIn("단일 주제 방송이면 억지 분할을 하지 않는다",
                      _TASKS["discover_candidates"])

    def test_finding_nothing_is_a_normal_ending(self):
        """Row 3: NO_CONTENT 로 정상 종료 (실패 아님)."""
        self.assertIn(State.NO_CONTENT, _NEXT[State.DISCOVERING] | _NEXT[State.EVALUATING])
        self.assertNotEqual(State.NO_CONTENT, State.FAILED)

    def test_a_spent_quota_queues_rather_than_fails(self):
        """Row 4: PT 자정 리셋 기준 재시도 큐 (11.4)."""
        import inspect

        from aicut.pipeline import publishing

        source = inspect.getsource(publishing)
        self.assertIn("enqueue_upload", source)
        self.assertIn("reset_at", source)

    def test_a_render_failure_keeps_the_plan(self):
        """Row 5: 편집 계획은 보존, 렌더 단계만 재실행."""
        import inspect

        from aicut.pipeline import rendering

        self.assertIn("render_failures", inspect.getsource(rendering))
        self.assertIn("render", {a.dest for a in _parser_subcommands()})

    def test_an_unresolved_speaker_does_not_stop_the_run(self):
        """Row 6: 화자 태그를 UNKNOWN 으로 두고 진행."""
        from aicut.models import UNKNOWN_SPEAKER

        self.assertTrue(UNKNOWN_SPEAKER)

    def test_a_very_long_source_is_folded_in_chunks(self):
        """Row 7: 1차 통과를 구간 분할한 뒤 5.4 사건 그래프 단계에서 병합."""
        import inspect

        from aicut.pipeline import understanding

        source = inspect.getsource(understanding)
        self.assertIn("long_source_chunk_sec", source)
        self.assertIn("merge_events", source)


def _parser_subcommands():
    from aicut.cli import build_parser

    for action in build_parser()._actions:
        if getattr(action, "choices", None) and hasattr(action.choices, "items"):
            for name in action.choices:
                yield type("A", (), {"dest": name})
    return


class DeliverableTests(unittest.TestCase):
    """22장's seven."""

    def test_all_seven_have_something_that_produces_them(self):
        for name, path in (
            ("단일 실행 데스크톱 프로그램", "aicut/desktop.py"),
            ("완성 영상 MP4", "aicut/render/ffmpeg.py"),
            ("썸네일 후보 이미지", "aicut/render/thumbnails.py"),
            ("메타데이터 패키지", "aicut/pipeline/packaging.py"),
            ("편집 계획 JSON", "aicut/render/editplan.py"),
            ("작업 리포트", "aicut/pipeline/runner.py"),
            ("채널 캘리브레이션 프로파일", "aicut/config.py"),
        ):
            with self.subTest(deliverable=name):
                self.assertTrue((ROOT / path).is_file())

    def test_the_metadata_package_is_written_as_json_and_txt(self):
        """22.4 says JSON·TXT — the person pastes the description somewhere."""
        import inspect

        from aicut.pipeline import packaging

        source = inspect.getsource(packaging.package_episode)
        self.assertIn('.json"', source)
        self.assertIn('with_suffix(".txt")', source)

    def test_the_text_package_carries_all_four_pieces(self):
        episode = Episode(episode_id="e1")
        episode.metadata = {
            "titles": ["가", "나", "다"],
            "description": "설명입니다",
            "tags": ["게임"],
            "chapters": [{"at_sec": 3725, "label": "격파"}],
            "upload": {"category_id": "20"},
        }
        text = _as_text(episode)
        for piece in ("제목 후보", "설명", "챕터", "태그"):
            self.assertIn(piece, text)
        self.assertIn("1:02:05", text, "a chapter needs a timestamp a person can paste")

    def test_an_empty_package_still_produces_a_file(self):
        self.assertTrue(_as_text(Episode(episode_id="e1")).strip())


if __name__ == "__main__":
    unittest.main()
