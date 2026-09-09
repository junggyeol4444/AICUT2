"""The Final Cut adapter, tested without Final Cut.

Final Cut Pro is macOS-only and is not in the environment this was written in,
so the import itself cannot be done here. What can: the document this writes is
compared cut for cut against the FCPXML `aicut export --format fcpxml`
produces for the same episode. Two implementations of one timeline drift, and
this is where that drift becomes a failure instead of a surprise at import.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from aicut.models import Cut, SubtitleLine
from aicut.render import exchange
from aicut.render.editmodel import from_edit_plan
from aicut.render.editplan import EditPlan

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
for path in (PLUGIN / "common", PLUGIN / "finalcut"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import aicut_finalcut as finalcut          # noqa: E402
from aicut_engine import EngineError         # noqa: E402
from aicut_model import ModelError          # noqa: E402


def _plan(cuts, fps=30, **kw):
    return EditPlan(episode_id="ep123456", project_id="proj",
                    source_path="/broadcasts/stream.mkv", cuts=cuts,
                    render_settings={"fps": fps, "width": 1920, "height": 1080}, **kw)


def _model(cuts, fps=30, duration=7200.0, **kw):
    plan = _plan(cuts, fps=fps, **kw)
    document = from_edit_plan(plan, source_duration_sec=duration, fps=fps)
    return json.loads(json.dumps(document.as_dict()))


def _clips(text):
    root = ElementTree.fromstring(text.split("<!DOCTYPE fcpxml>")[1])
    return [(c.get("offset"), c.get("start"), c.get("duration"))
            for c in root.iter("asset-clip")]


class SameTimelineAsTheExporterTests(unittest.TestCase):
    """The adapter writes what `aicut export` writes, or one of them is wrong."""

    def _both(self, cuts, fps=30, duration=600.0):
        plan = _plan(cuts, fps=fps)
        model = _model(cuts, fps=fps, duration=duration)
        mine = finalcut.to_fcpxml(model, model["sequences"][0], fps)
        theirs = exchange.to_fcpxml(plan, fps, source_duration_sec=duration)
        return mine, theirs

    def test_the_cuts_land_in_the_same_places(self):
        mine, theirs = self._both([
            Cut(sequence_order=1, source_start_sec=100.0, source_end_sec=110.0),
            Cut(sequence_order=2, source_start_sec=300.0, source_end_sec=320.0),
        ])
        self.assertEqual(_clips(mine), _clips(theirs))

    def test_a_removal_inside_a_cut_splits_it_the_same_way(self):
        """9.3: the spans pacing removed are gone from both documents."""
        mine, theirs = self._both([
            Cut(sequence_order=1, source_start_sec=300.0, source_end_sec=320.0,
                remove_spans=[[305.0, 307.0]]),
        ])
        self.assertEqual(len(_clips(mine)), 2)
        self.assertEqual(_clips(mine), _clips(theirs))

    def test_an_ntsc_rate_is_stated_the_same_way(self):
        """23.976 is 1001/24000, not 1/23.976. A decimal rate is not frame-exact
        and the importer rounds it whichever way it prefers."""
        mine, theirs = self._both(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0)],
            fps=23.976,
        )
        self.assertEqual(_clips(mine), _clips(theirs))
        self.assertIn('frameDuration="1001/24000s"', mine)

    def test_the_source_is_pointed_at_the_same_file(self):
        """It is the src that makes the timeline relink rather than come in offline."""
        mine, theirs = self._both(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        mine_src = ElementTree.fromstring(
            mine.split("<!DOCTYPE fcpxml>")[1]).find(".//media-rep").get("src")
        theirs_src = ElementTree.fromstring(
            theirs.split("<!DOCTYPE fcpxml>")[1]).find(".//media-rep").get("src")
        self.assertEqual(mine_src, theirs_src)
        self.assertTrue(mine_src.startswith("file://"))


class TheDocumentTests(unittest.TestCase):
    def test_timeline_order_is_kept_not_source_order(self):
        """2.4: a video may open on the moment that happened last."""
        model = _model([Cut(sequence_order=1, source_start_sec=500.0, source_end_sec=505.0),
                        Cut(sequence_order=2, source_start_sec=10.0, source_end_sec=15.0)])
        clips = _clips(finalcut.to_fcpxml(model, model["sequences"][0], 30))
        self.assertEqual([c[1] for c in clips], ["15000/30s", "300/30s"])

    def test_the_clips_are_laid_end_to_end(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=50.0, source_end_sec=53.0)])
        clips = _clips(finalcut.to_fcpxml(model, model["sequences"][0], 30))
        self.assertEqual([c[0] for c in clips], ["0/30s", "60/30s"])

    def test_it_is_the_fcpxml_version_final_cut_reads(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        text = finalcut.to_fcpxml(model, model["sequences"][0], 30)
        self.assertIn('<?xml version="1.0" encoding="UTF-8"?>', text)
        self.assertIn("<!DOCTYPE fcpxml>", text)
        self.assertIn('version="{}"'.format(finalcut.FCPXML_VERSION), text)

    def test_the_project_carries_the_name_the_model_gave_the_sequence(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        text = finalcut.to_fcpxml(model, model["sequences"][0], 30)
        project = ElementTree.fromstring(
            text.split("<!DOCTYPE fcpxml>")[1]).find(".//project")
        self.assertEqual(project.get("name"), model["sequences"][0]["name"])


class RefusalTests(unittest.TestCase):
    def test_a_rate_fcpxml_cannot_state_exactly_is_refused(self):
        """Writing 1/31.7 would be a lie the importer rounds silently."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        with self.assertRaises(ModelError) as raised:
            finalcut.to_fcpxml(model, model["sequences"][0], 31.7)
        self.assertIn("not a frame rate", str(raised.exception))

    def test_a_model_with_no_rate_asks_rather_than_picking_one(self):
        """Every cut is placed against this number; a guess slides all of them."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        model["sequences"][0]["fps"] = None
        with self.assertRaises(ModelError) as raised:
            finalcut.sequence_fps(model["sequences"][0])
        self.assertIn("--fps", str(raised.exception))

    def test_the_rate_on_the_command_line_wins(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        model["sequences"][0]["fps"] = None
        self.assertEqual(finalcut.sequence_fps(model["sequences"][0], 25), 25.0)

    def test_a_sequence_of_nothing_but_slivers_is_an_error_not_an_empty_project(self):
        model = _model([Cut(sequence_order=1, source_start_sec=5.0, source_end_sec=5.001)])
        with self.assertRaises(ModelError) as raised:
            finalcut.to_fcpxml(model, model["sequences"][0], 30)
        self.assertIn("shorter than one frame", str(raised.exception))

    def test_an_edit_plan_is_named_as_the_wrong_document(self):
        """37장 has the adapter read the model, not the plan."""
        from aicut_model import validated

        with self.assertRaises(ModelError) as raised:
            validated({"cuts": []})
        self.assertIn("not a Common Edit Model", str(raised.exception))


class WhatTheFormatCannotCarryTests(unittest.TestCase):
    """Silence about a dropped thing is the failure; saying it is the fix."""

    def _write(self, model, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            import io
            import contextlib

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                finalcut.write(model, model["sequences"][0],
                               str(Path(tmp) / "seq.fcpxml"), **kw)
            return out.getvalue()

    def test_a_span_shorter_than_a_frame_is_named(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=5.0, source_end_sec=5.001)])
        self.assertIn("shorter than one frame", self._write(model))

    def test_the_captions_and_the_placed_audio_are_named(self):
        model = _model(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)],
            subtitles=[SubtitleLine(0.0, 2.0, "first")],
            structure={"bgm": {"path": "/music/bed.mp3"}},
        )
        said = self._write(model)
        self.assertIn("1 caption(s)", said)
        self.assertIn("audio placement(s)", said)
        self.assertIn("/music/bed.mp3", said)

    def test_the_markers_are_named(self):
        """29장 has the person step through the result; the marks are the model's."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        self.assertIn("marker(s) in the model are not in the XML", self._write(model))

    def test_the_file_is_written_where_it_was_asked_for(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "seq.fcpxml"
            import contextlib
            import io

            with contextlib.redirect_stdout(io.StringIO()):
                finalcut.write(model, model["sequences"][0], str(target))
            self.assertTrue(target.exists())
            self.assertIn("<fcpxml", target.read_text(encoding="utf-8"))


class TheButtonTests(unittest.TestCase):
    """4장 through the adapter that has no editor to press it in.

    Final Cut cannot tell a script what is on a timeline, so the broadcast is
    named on the command line - the rest is the same button.
    """

    class FakeEngine:
        def __init__(self, episodes, states=None, model=None):
            self._episodes = episodes
            self._states = list(states or [{"running": False, "project_id": "p1"}])
            self._model = model
            self.submitted = []
            self.asked_modes = []

        def submit(self, source_path, **options):
            self.submitted.append(source_path)
            return {"job_id": "j1", "project_id": "p1"}

        def job(self, job_id):
            return self._states.pop(0) if len(self._states) > 1 else self._states[0]

        def episodes(self, project_id):
            return list(self._episodes)

        def edit_model(self, episode_id, mode="new_sequence"):
            self.asked_modes.append(mode)
            return self._model

    def _run(self, engine, **kwargs):
        import contextlib
        import io

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(out):
                written = finalcut.build_from_engine(
                    "/broadcasts/stream.mkv", out_dir=tmp, poll_sec=0,
                    engine=engine, open_after=False, **kwargs,
                )
            return written, out.getvalue()

    def test_it_writes_one_document_per_episode(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        engine = self.FakeEngine([{"episode_id": "e1"}, {"episode_id": "e2"}], model=model)

        written, _said = self._run(engine)

        self.assertEqual(len(written), 2)
        self.assertEqual(engine.submitted, ["/broadcasts/stream.mkv"])

    def test_finding_nothing_is_a_normal_ending(self):
        """16장: 제작 가치 있는 콘텐츠 없음 is not a failure."""
        written, said = self._run(self.FakeEngine([]))

        self.assertEqual(written, [])
        self.assertIn("16장", said)

    def test_a_run_the_pipeline_failed_is_not_called_finding_nothing(self):
        """Measured against a real engine: a missing speech recogniser came back
        as state FAILED with an empty `error`, and reading `error` alone
        reported it as 16장's normal ending."""
        engine = self.FakeEngine([], states=[{
            "running": False, "state": "FAILED", "error": "",
            "report": {"error": "whisperx is not installed"},
        }])

        with self.assertRaises(EngineError) as raised:
            self._run(engine)

        self.assertIn("whisperx is not installed", str(raised.exception))

    def test_the_persons_mode_reaches_the_engine(self):
        """25장's choice is theirs; the adapter passes it through."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        engine = self.FakeEngine([{"episode_id": "e1"}], model=model)

        self._run(engine, mode="edit_current")

        self.assertEqual(engine.asked_modes, ["edit_current"])

    def test_each_stage_the_engine_names_is_reported_once(self):
        """26장's progress panel is this output."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        engine = self.FakeEngine(
            [{"episode_id": "e1"}],
            states=[{"running": True, "state": "PARSING"},
                    {"running": True, "state": "PARSING"},
                    {"running": True, "state": "UNDERSTANDING"},
                    {"running": False, "state": "REVIEW_PENDING", "project_id": "p1"}],
            model=model,
        )

        _written, said = self._run(engine)

        stages = [line.strip() for line in said.splitlines() if line.strip().isupper()]
        self.assertEqual(stages, ["PARSING", "UNDERSTANDING", "REVIEW_PENDING"])


class HonestyTests(unittest.TestCase):
    def test_it_says_the_import_was_never_run(self):
        """Not decoration: it is the difference between an adapter that was
        tested and one that was written from documentation."""
        source = (PLUGIN / "finalcut" / "aicut_finalcut.py").read_text(encoding="utf-8")
        self.assertIn("NOT VERIFIED IN FINAL CUT", source)

    def test_it_does_not_claim_to_import_on_another_platform(self):
        import contextlib
        import io

        if sys.platform == "darwin":
            self.skipTest("this machine has Final Cut's platform")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handed = finalcut.open_in_final_cut("/tmp/seq.fcpxml")
        self.assertFalse(handed)
        self.assertIn("File > Import > XML", out.getvalue())


if __name__ == "__main__":
    unittest.main()
