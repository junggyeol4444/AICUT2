"""The fourteen findings of the fourth Codex review, each with what it cost."""

from __future__ import annotations

import unittest

from aicut.models import Cut, Episode, SubtitleLine


class EngineSubmitTests(unittest.TestCase):
    """An editor plugin wants the plan, not an encoded file."""

    def test_the_plugin_does_not_ask_the_engine_to_render(self):
        import sys
        from pathlib import Path

        common = Path(__file__).resolve().parent.parent / "plugin" / "common"
        if str(common) not in sys.path:
            sys.path.insert(0, str(common))
        from aicut_engine import Engine

        sent = {}

        class Recording(Engine):
            def _call(self, method, path, body=None):
                sent.update({"method": method, "path": path, "body": body})
                return {"job_id": "j1"}

        Recording().submit("/live.mkv")
        self.assertEqual(sent["body"], {"source": "/live.mkv", "render": False})

    def test_a_caller_that_does_want_a_render_can_say_so(self):
        import sys
        from pathlib import Path

        common = Path(__file__).resolve().parent.parent / "plugin" / "common"
        if str(common) not in sys.path:
            sys.path.insert(0, str(common))
        from aicut_engine import Engine

        sent = {}

        class Recording(Engine):
            def _call(self, method, path, body=None):
                sent.update({"body": body})
                return {}

        Recording().submit("/live.mkv", render=True)
        self.assertTrue(sent["body"]["render"])


class ChaptersReachTheUploadTests(unittest.TestCase):
    """YouTube has no chapter field: it reads them out of the description."""

    def _description(self, **metadata):
        from aicut.pipeline.publishing import _description_with_chapters

        return _description_with_chapters(Episode(project_id="p", metadata=metadata))

    def test_every_chapter_is_in_the_description_that_is_uploaded(self):
        said = self._description(
            description="오늘 방송",
            chapters=[{"at_sec": 0, "title": "시작"},
                      {"at_sec": 75, "title": "보스전"},
                      {"at_sec": 3723, "title": "마무리"}],
        )
        self.assertIn("0:00 시작", said)
        self.assertIn("1:15 보스전", said)
        self.assertIn("1:02:03 마무리", said)

    def test_a_description_that_already_carries_them_is_left_alone(self):
        """Two copies of a chapter list is worse than none."""
        said = self._description(
            description="0:00 시작\n1:15 보스전",
            chapters=[{"at_sec": 0, "title": "시작"}, {"at_sec": 75, "title": "보스전"}],
        )
        self.assertEqual(said, "0:00 시작\n1:15 보스전")

    def test_no_chapters_means_no_change(self):
        self.assertEqual(self._description(description="그냥 설명"), "그냥 설명")


class ScopeTests(unittest.TestCase):
    """11.3 uploads privately and a person makes it public - that is a write."""

    def test_the_scopes_include_one_that_can_publish(self):
        from aicut.intelligence.youtube import SCOPES

        self.assertIn("https://www.googleapis.com/auth/youtube.force-ssl", SCOPES)

    def test_a_token_granted_fewer_scopes_is_not_reused(self):
        import inspect

        from aicut.intelligence.youtube import load_credentials

        source = inspect.getsource(load_credentials)
        self.assertIn("_has_every_scope", source)


class UiDefaultsTests(unittest.TestCase):
    def test_the_ui_looks_at_the_picture_by_default(self):
        """5.2 reads 화면과 소리를 같이 본다, and the browser sends no such field."""
        import inspect

        from aicut.ui.server import UiServer

        source = inspect.getsource(UiServer)
        self.assertIn('body.get("sample_frames", True)', source)

    def test_the_ui_has_a_token_path_to_hand_to_oauth(self):
        """`aicut ui` takes no --token, and `Path(None)` is a TypeError."""
        import inspect

        from aicut.ui.server import UiServer

        source = inspect.getsource(UiServer._youtube)
        self.assertIn('self.workspace / "youtube_token.json"', source)


class SupersededEpisodeTests(unittest.TestCase):
    """A resumed run plans again; the previous generation must not stand."""

    def setUp(self):
        from aicut.db.store import Store

        self.store = Store(":memory:")

    def _episode(self, status):
        from aicut.models import Project

        self.store.create_project(Project(project_id="p1", file_path="/live.mkv"))
        episode = Episode(project_id="p1", review_status=status)
        self.store.save_episode(episode)
        return episode

    def test_a_pending_episode_is_retired(self):
        self._episode("pending")
        self.assertEqual(self.store.supersede_episodes("p1"), 1)
        self.assertEqual(self.store.episodes("p1")[0].review_status, "superseded")

    def test_a_published_one_is_left_alone(self):
        """It is on the channel; this run does not get to rewrite that."""
        self._episode("published")
        self.assertEqual(self.store.supersede_episodes("p1"), 0)
        self.assertEqual(self.store.episodes("p1")[0].review_status, "published")

    def test_a_rejected_one_is_left_alone(self):
        self._episode("rejected")
        self.assertEqual(self.store.supersede_episodes("p1"), 0)

    def test_a_superseded_episode_does_not_hold_the_project_open(self):
        from aicut.pipeline.publishing import _SETTLED

        self.assertIn("superseded", _SETTLED)


class NtscTimecodeInTheAvidAdapterTests(unittest.TestCase):
    """The adapter is checked against the exporter, and both were wrong."""

    def setUp(self):
        import sys
        from pathlib import Path

        plugin = Path(__file__).resolve().parent.parent / "plugin"
        for path in (plugin / "common", plugin / "avid"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

    def test_an_hour_of_2997_is_the_frame_the_media_is_on(self):
        import aicut_avid as avid

        self.assertEqual(avid.timecode(3600.0, 29.97), "00:59:56:12")

    def test_it_still_agrees_with_the_exporter(self):
        from aicut.render import exchange

        import aicut_avid as avid

        for fps in (23.976, 24, 25, 29.97, 30, 59.94, 60):
            for seconds in (0.0, 1.5, 3600.0, 21600.0):
                with self.subTest(fps=fps, seconds=seconds):
                    self.assertEqual(avid.timecode(seconds, fps),
                                     exchange.timecode(seconds, fps))


class TransitionLengthTests(unittest.TestCase):
    """The renderer reads `sec`; the model read `duration` and got the default."""

    def test_the_model_reads_the_key_the_renderer_writes(self):
        from aicut.render.editmodel import from_edit_plan
        from aicut.render.editplan import EditPlan

        cut = Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0,
                  visual_effect={"transition": {"in": "fade", "sec": 2.0}})
        plan = EditPlan(episode_id="e", project_id="p", source_path="/live.mkv",
                        cuts=[cut], render_settings={"fps": 30})
        model = from_edit_plan(plan, source_duration_sec=60.0, fps=30)
        clip = model.sequences[0].tracks[0].clips[0]
        self.assertEqual([t.duration_sec for t in clip.transitions], [2.0])


class SubtitleStyleTravelsWithThePlanTests(unittest.TestCase):
    """8.2: the plan is what the render executes, style included."""

    def test_the_renderer_prefers_the_plans_own_style(self):
        import inspect

        from aicut.pipeline import rendering

        source = inspect.getsource(rendering)
        self.assertIn('render_settings or {}).get("subtitle_style_profile")', source)

    def test_planning_records_it(self):
        import inspect

        from aicut.pipeline import planning

        self.assertIn('settings_dict["subtitle_style_profile"]',
                      inspect.getsource(planning))


class CliUsesTheProjectsProfileTests(unittest.TestCase):
    """review / upload / retry were reading whatever --profile defaulted to."""

    def test_the_context_resolves_the_projects_own_profile(self):
        import inspect

        from aicut.cli import _project_profile

        source = inspect.getsource(_project_profile)
        self.assertIn("project.profile_id", source)
        self.assertIn("asked", source)


class AdaptersSayWhatTheyDroppedTests(unittest.TestCase):
    """Silence about a dropped zoom is the failure; saying it is the fix."""

    def test_the_resolve_adapter_counts_the_effects_it_cannot_apply(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "plugin" / "resolve"
                  / "aicut_resolve.py").read_text(encoding="utf-8")
        self.assertIn("effect(s)/transition(s) the model asks for are not applied", source)

    def test_the_premiere_adapter_does_too(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "plugin" / "premiere"
                  / "aicut_premiere.jsx").read_text(encoding="utf-8")
        self.assertIn("effect(s)/transition(s) the model asks for are not applied", source)


class ReadmeTests(unittest.TestCase):
    def test_the_loop_b_example_uses_options_the_cli_has(self):
        from pathlib import Path

        from aicut.cli import build_parser

        line = [l for l in (Path(__file__).resolve().parent.parent / "README.md")
                .read_text(encoding="utf-8").splitlines()
                if l.startswith("aicut learn pairs")][0]
        learn = build_parser()._subparsers._group_actions[0].choices["learn"]
        known = {o for a in learn._actions for o in a.option_strings}
        for word in line.split():
            if word.startswith("--"):
                with self.subTest(option=word):
                    self.assertIn(word, known)


class UiStreamsInsteadOfDownloadingTests(unittest.TestCase):
    def test_an_unauthenticated_preview_points_at_the_url(self):
        """The server answers Range requests; the blob never used them."""
        from pathlib import Path

        page = (Path(__file__).resolve().parent.parent / "aicut" / "ui" / "static"
                / "index.html").read_text(encoding="utf-8")
        self.assertIn("if (!apiKey()) { v.src = path; v.play(); return; }", page)


if __name__ == "__main__":
    unittest.main()
