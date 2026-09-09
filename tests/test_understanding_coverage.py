"""What 5장 asks the two passes for, and where the answers go.

5.1 says what each pass is looking for, 5.2 names what it reads on the way
through, 5.4 says the passes are not independent fragments, and 5.5 gives the
semantic structure they build. A missing item here is a question nobody asks,
so the clauses are the test.
"""

import unittest

from aicut.db.store import Store
from aicut.llm.prompts import _TASKS
from aicut.models import WindowSummary
from aicut.pipeline.understanding import _memory


class FirstPassPromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = _TASKS["summarize_window"]

    def test_the_five_questions_of_the_first_pass(self):
        for question in ("지금 무슨 상황인가", "누가 있는가", "무슨 얘기가",
                         "화면에서 무슨 일이 벌어지는가", "어디가 다시 볼 만한가"):
            self.assertIn(question, self.prompt, f"5.1: {question} is not asked")

    def test_every_screen_item_of_5_2(self):
        for item in ("인물", "표정", "동작", "게임 상황", "게임 결과",
                     "채팅·후원", "화면 사건"):
            self.assertIn(item, self.prompt, f"5.2 화면: {item} is not asked for")

    def test_every_voice_item_of_5_2(self):
        for item in ("발화 내용", "대화 흐름", "말투", "감정", "중요 발언"):
            self.assertIn(item, self.prompt, f"5.2 음성: {item} is not asked for")

    def test_every_audio_item_of_5_2(self):
        for item in ("웃음", "비명", "환호", "침묵", "효과음", "BGM 변화"):
            self.assertIn(item, self.prompt, f"5.2 오디오: {item} is not asked for")

    def test_the_three_branches_of_5_5_the_pass_owns(self):
        for branch in ("conversations", "changes", "temporal_links"):
            self.assertIn(branch, self.prompt, f"5.5: {branch} is not asked for")

    def test_the_measured_signals_are_offered_as_hints_not_conclusions(self):
        """18장: what a moment is stays the AI's. A tension number is not a verdict."""
        self.assertIn("not conclusions", self.prompt)


class SecondPassPromptTests(unittest.TestCase):
    def test_the_four_questions_of_the_second_pass(self):
        prompt = _TASKS["detail_window"]
        for question in ("정확히 언제 시작하고 끝나는가", "대사와 반응의 정확한 타이밍",
                         "표정과 동작", "편집에 필요한 세부 정보"):
            self.assertIn(question, prompt, f"5.1 2차: {question} is not asked")

    def test_the_second_pass_still_reads_screen_and_sound_together(self):
        self.assertIn("5.2", _TASKS["detail_window"])


class EventPromptTests(unittest.TestCase):
    def test_the_5_4_example_shape_is_given(self):
        prompt = _TASKS["build_events"]
        for role in ("처음 언급", "관련 대화", "재언급", "갈등", "결과"):
            self.assertIn(role, prompt, f"5.4: {role} is not shown as a moment's role")

    def test_distance_in_time_is_not_a_reason_to_split_an_event(self):
        """5.4 -> 2.4: an event scattered across hours is one event."""
        self.assertIn("do not split one", _TASKS["build_events"])

    def test_the_links_the_passes_noticed_are_what_events_fold_along(self):
        self.assertIn("temporal_links", _TASKS["build_events"])


class MemoryTests(unittest.TestCase):
    """5.4: 앞에서 본 내용을 누적한 상태로 뒤를 해석한다."""

    def _windows(self):
        return [
            WindowSummary(
                start_sec=0, end_sec=300, summary="사건이 시작됨",
                people=["호스트"], topics=["보스"],
                changes=[{"what": "분위기", "from": "잡담", "to": "집중"}],
            ),
            *[WindowSummary(start_sec=300 * i, end_sec=300 * (i + 1), summary=f"w{i}")
              for i in range(1, 12)],
            WindowSummary(
                start_sec=3600, end_sec=3900, summary="아까 그 얘기 다시",
                temporal_links=[{"refers_to_sec": 120, "what": "보스 얘기 재언급"}],
            ),
        ]

    def test_a_callback_from_an_hour_ago_is_still_in_view(self):
        memory = _memory(self._windows())
        self.assertTrue(memory["callbacks_so_far"])
        self.assertEqual(memory["callbacks_so_far"][-1]["refers_to_sec"], 120)

    def test_a_change_older_than_the_recent_windows_survives(self):
        """The first window is long out of `recent_windows` by the twelfth."""
        memory = _memory(self._windows())
        self.assertEqual(memory["changes_so_far"][0]["what"], "분위기")
        self.assertEqual(memory["changes_so_far"][0]["at_sec"], 0)

    def test_every_link_carries_the_time_it_was_noticed_at(self):
        for link in _memory(self._windows())["callbacks_so_far"]:
            self.assertIn("at_sec", link)

    def test_an_empty_broadcast_is_not_an_error(self):
        memory = _memory([])
        self.assertEqual(memory["callbacks_so_far"], [])
        self.assertEqual(memory["changes_so_far"], [])


class WindowPersistenceTests(unittest.TestCase):
    def test_the_5_5_branches_survive_the_database(self):
        window = WindowSummary(
            start_sec=0, end_sec=300, summary="s",
            conversations=[{"who": ["호스트", "게스트"], "about": "보스 공략"}],
            changes=[{"what": "게임 상황", "from": "패배", "to": "승리"}],
            temporal_links=[{"refers_to_sec": 32, "what": "아까 그 사건"}],
        )
        store = Store(":memory:")
        try:
            store.create_project(_project())
            store.replace_windows("p1", [window])
            back = store.windows("p1")[0]
        finally:
            store.close()
        self.assertEqual(back.conversations, window.conversations)
        self.assertEqual(back.changes, window.changes)
        self.assertEqual(back.temporal_links, window.temporal_links)

    def test_a_workspace_written_before_these_columns_existed_still_opens(self):
        """CREATE TABLE IF NOT EXISTS leaves an old database on its old shape."""
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.execute(
                "CREATE TABLE tb_window_summary (window_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " project_id TEXT, start_sec REAL, end_sec REAL, summary TEXT, people TEXT,"
                " topics TEXT, screen TEXT, notable INTEGER, notable_reason TEXT,"
                " tension_peak REAL, markers TEXT)"
            )
            old.commit()
            old.close()

            store = Store(path)
            try:
                columns = {r["name"] for r in
                           store.conn.execute("PRAGMA table_info(tb_window_summary)")}
            finally:
                store.close()
        for added in ("conversations", "changes", "temporal_links"):
            self.assertIn(added, columns, f"an existing workspace would fail on {added}")


def _project():
    from aicut.models import Project

    return Project(project_id="p1", file_path="/x/broadcast.mp4", duration_sec=3600.0)


if __name__ == "__main__":
    unittest.main()
