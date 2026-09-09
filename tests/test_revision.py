"""AI Timeline 수정 (플러그인 기획안 30장, MVP 7).

    "초반을 20초 정도 줄여줘"
        -> 현재 Timeline 분석 -> 불필요한 부분 탐색
        -> 새로운 편집 계획 생성 -> Timeline 수정

Which twenty seconds to lose is the AI's judgement (18장). Whether what comes
back is a timeline of this broadcast at all is not - these are that check.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aicut.db.store import Store
from aicut.llm.mock import MockProducer
from aicut.models import Cut, Episode, Project, SubtitleLine
from aicut.pipeline.context import RunContext
from aicut.pipeline.revision import RevisionRefused, revise
from aicut.render.editplan import EditPlan


class _Answering(MockProducer):
    """A producer that returns one prepared revision, so the check is on us."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.asked = None

    def revise_timeline(self, payload):
        self.asked = payload
        return self.answer


def _cut(order, start, end, **kw):
    return Cut(sequence_order=order, source_start_sec=start, source_end_sec=end, **kw)


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.store = Store(":memory:")
        self.project = Project(project_id="p1", file_path="/live.mkv", duration_sec=600.0)
        self.store.create_project(self.project)
        self.episode = Episode(project_id="p1", timeline=[
            _cut(1, 0.0, 30.0, scene_role="intro"),
            _cut(2, 100.0, 130.0, scene_role="core"),
            _cut(3, 300.0, 320.0, scene_role="result"),
        ])
        self.store.save_episode(self.episode)
        self.plan = EditPlan(
            episode_id=self.episode.episode_id, project_id="p1",
            source_path="/live.mkv", cuts=list(self.episode.timeline),
            subtitles=[SubtitleLine(0.0, 2.0, "안녕하세요")],
            render_settings={"fps": 30},
        )
        self.plan_path = self.workspace / "p1" / "plans" / f"{self.episode.episode_id}.json"
        self.plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.plan.save(self.plan_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, producer):
        return RunContext(project=self.project, store=self.store, profile=None,
                          producer=producer, workspace=self.workspace)

    def _revise(self, answer, request="초반을 20초 정도 줄여줘"):
        producer = _Answering(answer)
        return producer, revise(self._ctx(producer), self.episode, request)

    # -- 30장's own example ---------------------------------------------------
    def test_the_opening_can_be_shortened_by_asking(self):
        """30장: 사용자 요청 "초반을 20초 정도 줄여줘"."""
        producer, result = self._revise({
            "rationale": "opened later; the first 20s was setup",
            "cuts": [
                {"sequence_order": 1, "source_start_sec": 20.0, "source_end_sec": 30.0,
                 "role": "intro"},
                {"sequence_order": 2, "source_start_sec": 100.0, "source_end_sec": 130.0,
                 "role": "core"},
                {"sequence_order": 3, "source_start_sec": 300.0, "source_end_sec": 320.0,
                 "role": "result"},
            ],
        })
        self.assertEqual(result["duration_before_sec"], 80.0)
        self.assertEqual(result["duration_after_sec"], 60.0)
        self.assertEqual(result["cuts_after"], 3)

    def test_the_words_and_the_timeline_are_what_it_is_asked_about(self):
        """A request about "그 부분" is about something somebody said."""
        producer, _result = self._revise({"cuts": [
            {"sequence_order": 1, "source_start_sec": 0.0, "source_end_sec": 30.0}]})
        payload = producer.asked
        self.assertEqual(payload["request"], "초반을 20초 정도 줄여줘")
        self.assertEqual(len(payload["cuts"]), 3)
        self.assertEqual(payload["source_duration_sec"], 600.0)
        self.assertEqual(payload["subtitles"][0]["text"], "안녕하세요")
        self.assertEqual(payload["cuts"][1]["output_start_sec"], 30.0)

    def test_the_new_timeline_is_written_to_the_plan_and_the_episode(self):
        self._revise({"cuts": [
            {"sequence_order": 1, "source_start_sec": 300.0, "source_end_sec": 320.0}]})
        saved = EditPlan.load(self.plan_path)
        self.assertEqual([(c.source_start_sec, c.source_end_sec) for c in saved.cuts],
                         [(300.0, 320.0)])
        stored = self.store.get_episode(self.episode.episode_id)
        self.assertEqual(len(stored.timeline), 1)

    def test_the_previous_plan_is_kept(self):
        """22.5 lets a person disagree, and they cannot if it is gone."""
        _producer, result = self._revise({"cuts": [
            {"sequence_order": 1, "source_start_sec": 300.0, "source_end_sec": 320.0}]})
        before = EditPlan.load(result["previous_plan"])
        self.assertEqual(len(before.cuts), 3)

    def test_what_changed_is_recorded_in_the_plan(self):
        self._revise({"rationale": "cut the setup", "cuts": [
            {"sequence_order": 1, "source_start_sec": 300.0, "source_end_sec": 320.0}]})
        saved = EditPlan.load(self.plan_path)
        revisions = saved.provenance["revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["request"], "초반을 20초 정도 줄여줘")
        self.assertEqual(revisions[0]["rationale"], "cut the setup")
        self.assertEqual(revisions[0]["cuts_before"], 3)

    def test_a_rendered_file_is_reported_as_out_of_date(self):
        """30장 has them check the result; the old file is not the result."""
        self.episode.output_mp4_path = "/w/ep.mp4"
        self.store.save_episode(self.episode)
        _producer, result = self._revise({"cuts": [
            {"sequence_order": 1, "source_start_sec": 0.0, "source_end_sec": 30.0}]})
        self.assertTrue(result["rendered_output_is_stale"])

    def test_a_removal_inside_a_cut_survives_the_round_trip(self):
        _producer, result = self._revise({"cuts": [
            {"sequence_order": 1, "source_start_sec": 0.0, "source_end_sec": 30.0,
             "remove_spans": [[10.0, 12.0]]}]})
        self.assertEqual(result["duration_after_sec"], 28.0)

    # -- what it refuses ------------------------------------------------------
    def test_a_cut_past_the_end_of_the_broadcast_is_refused(self):
        """That material does not exist, and clamping it would be a different
        edit made silently here."""
        with self.assertRaises(RevisionRefused) as raised:
            self._revise({"cuts": [
                {"sequence_order": 1, "source_start_sec": 590.0, "source_end_sec": 900.0}]})
        self.assertIn("past the end", str(raised.exception))
        self.assertEqual(len(EditPlan.load(self.plan_path).cuts), 3)

    def test_a_span_that_ends_before_it_starts_is_refused(self):
        with self.assertRaises(RevisionRefused):
            self._revise({"cuts": [
                {"sequence_order": 1, "source_start_sec": 30.0, "source_end_sec": 10.0}]})

    def test_an_empty_timeline_is_refused(self):
        with self.assertRaises(RevisionRefused) as raised:
            self._revise({"cuts": []})
        self.assertIn("no timeline", str(raised.exception))

    def test_a_timeline_pacing_would_empty_is_refused(self):
        with self.assertRaises(RevisionRefused) as raised:
            self._revise({"cuts": [
                {"sequence_order": 1, "source_start_sec": 0.0, "source_end_sec": 10.0,
                 "remove_spans": [[0.0, 10.0]]}]})
        self.assertIn("empty", str(raised.exception))

    def test_the_models_own_refusal_is_passed_through_untouched(self):
        with self.assertRaises(RevisionRefused) as raised:
            self._revise({"refusal": "이 요청으로는 남는 장면이 없다", "cuts": []})
        self.assertEqual(str(raised.exception), "이 요청으로는 남는 장면이 없다")

    def test_an_empty_request_is_refused_before_anything_is_asked(self):
        producer = _Answering({"cuts": []})
        with self.assertRaises(RevisionRefused):
            revise(self._ctx(producer), self.episode, "   ")
        self.assertIsNone(producer.asked)

    def test_an_episode_with_no_plan_says_so(self):
        self.plan_path.unlink()
        with self.assertRaises(RevisionRefused) as raised:
            self._revise({"cuts": []})
        self.assertIn("nothing to revise", str(raised.exception))

    def test_nothing_is_written_when_it_is_refused(self):
        before = self.plan_path.read_text(encoding="utf-8")
        with self.assertRaises(RevisionRefused):
            self._revise({"refusal": "no"})
        self.assertEqual(self.plan_path.read_text(encoding="utf-8"), before)

    def test_the_mock_backend_refuses_rather_than_inventing_an_edit(self):
        """It judges nothing; a timeline from it would be mistaken for an answer."""
        with self.assertRaises(RevisionRefused) as raised:
            revise(self._ctx(MockProducer()), self.episode, "초반 줄여줘")
        self.assertIn("judges nothing", str(raised.exception))


class TheWayInTests(unittest.TestCase):
    """One request, three ways to make it: the CLI, the engine, the plugin."""

    def test_the_cli_has_the_command(self):
        from aicut.cli import build_parser

        choices = build_parser()._subparsers._group_actions[0].choices
        self.assertIn("revise", choices)

    def test_the_engine_serves_it(self):
        import inspect

        from aicut.ui.server import UiServer, _Handler

        self.assertIn("/revise", inspect.getsource(_Handler))
        self.assertTrue(hasattr(UiServer, "revise"))

    def test_the_plugin_connector_can_ask_for_it(self):
        import sys
        from pathlib import Path as _Path

        common = _Path(__file__).resolve().parent.parent / "plugin" / "common"
        if str(common) not in sys.path:
            sys.path.insert(0, str(common))
        from aicut_engine import Engine

        sent = {}

        class Recording(Engine):
            def _call(self, method, path, body=None):
                sent.update({"method": method, "path": path, "body": body})
                return {}

        Recording().revise("ep1", "초반 20초 줄여줘")
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["path"], "/api/episodes/ep1/revise")
        self.assertEqual(sent["body"], {"request": "초반 20초 줄여줘"})

    def test_the_javascript_connector_can_too(self):
        import shutil
        import subprocess
        from pathlib import Path as _Path

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is needed to run the plugin's own code")
        engine = _Path(__file__).resolve().parent.parent / "plugin" / "common" / "aicut_engine.js"
        script = (
            "var e = require({});"
            "var sent; var eng = new e.Engine({{transport: function (r) {{ sent = r;"
            " return 'HTTP/1.0 200 OK\\r\\n\\r\\n{{}}'; }}}});"
            "eng.revise('ep1', 'x');"
            "process.stdout.write(sent.split('\\r\\n')[0]);"
        ).format(json.dumps(str(engine)))
        done = subprocess.run([node, "-e", script], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "POST /api/episodes/ep1/revise HTTP/1.0")


if __name__ == "__main__":
    unittest.main()
