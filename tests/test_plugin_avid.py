"""The Avid adapter, tested without Media Composer.

Avid is not in the environment this was written in, so the import cannot be
done here. What can: the EDL this writes is compared event for event with the
one `aicut export --format edl` produces for the same episode. An EDL is cuts
and timecode and nothing else, so a disagreement between the two is a cut in the
wrong place - which is exactly the thing nobody notices until the conform.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from aicut.models import Cut, SubtitleLine
from aicut.render import exchange
from aicut.render.editmodel import from_edit_plan
from aicut.render.editplan import EditPlan

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
for path in (PLUGIN / "common", PLUGIN / "avid"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import aicut_avid as avid                     # noqa: E402
from aicut_engine import EngineError          # noqa: E402
from aicut_model import ModelError            # noqa: E402


def _plan(cuts, fps=30, source="/broadcasts/stream.mkv", **kw):
    return EditPlan(episode_id="ep123456", project_id="proj", source_path=source,
                    cuts=cuts, render_settings={"fps": fps}, **kw)


def _model(cuts, fps=30, duration=7200.0, source="/broadcasts/stream.mkv", **kw):
    plan = _plan(cuts, fps=fps, source=source, **kw)
    return json.loads(json.dumps(
        from_edit_plan(plan, source_duration_sec=duration, fps=fps).as_dict()
    ))


def _events(text):
    return [line for line in text.splitlines()
            if line[:3].isdigit() or line.startswith("*")]


class SameEdlAsTheExporterTests(unittest.TestCase):
    """The adapter writes what `aicut export` writes, or one of them is wrong."""

    def _both(self, cuts, fps=30, source="/broadcasts/stream.mkv"):
        plan = _plan(cuts, fps=fps, source=source)
        model = _model(cuts, fps=fps, source=source)
        mine = avid.to_edl(model, model["sequences"][0], fps, title=plan.episode_id)
        return mine, exchange.to_edl(plan, fps)

    def test_the_cuts_land_on_the_same_timecodes(self):
        mine, theirs = self._both([
            Cut(sequence_order=1, source_start_sec=100.0, source_end_sec=110.0),
            Cut(sequence_order=2, source_start_sec=300.0, source_end_sec=320.0),
        ])
        self.assertEqual(mine, theirs)

    def test_a_removal_inside_a_cut_becomes_two_events_in_both(self):
        """9.3: the spans pacing removed are gone from both documents."""
        mine, theirs = self._both([
            Cut(sequence_order=1, source_start_sec=300.0, source_end_sec=320.0,
                remove_spans=[[305.0, 307.0]]),
        ])
        self.assertEqual(len(_events(mine)), 4)      # two events, each with its name line
        self.assertEqual(mine, theirs)

    def test_timeline_order_is_kept_not_source_order(self):
        """2.4: a video may open on the moment that happened last."""
        mine, theirs = self._both([
            Cut(sequence_order=1, source_start_sec=500.0, source_end_sec=505.0),
            Cut(sequence_order=2, source_start_sec=10.0, source_end_sec=15.0),
        ])
        self.assertIn("00:08:20:00", mine.splitlines()[2])
        self.assertEqual(mine, theirs)

    def test_an_ntsc_rate_is_counted_the_same_way(self):
        """23.976 is counted at 24 frames, which is what non-drop-frame is."""
        mine, theirs = self._both(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0)], fps=23.976)
        self.assertEqual(mine, theirs)

    def test_the_reel_name_is_the_same_eight_characters(self):
        """An EDL names a reel, not a file. A different reel is a second relink."""
        mine, theirs = self._both(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)],
            source="/방송/2026-08-19 [하이라이트].mkv")
        self.assertEqual(mine, theirs)
        # The digits survive; the Korean and the brackets do not, because a reel
        # name is eight characters of A-Z0-9 in practice and every editor
        # mangles anything else differently.
        self.assertIn("20260819", mine)

    def test_a_name_with_nothing_usable_in_it_still_gets_a_reel(self):
        mine, theirs = self._both(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)],
            source="/방송/하이라이트.mkv")
        self.assertEqual(mine, theirs)
        self.assertIn("AICUT", mine)


class TheDocumentTests(unittest.TestCase):
    def test_it_is_the_header_an_edl_importer_expects(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        text = avid.to_edl(model, model["sequences"][0], 30)
        self.assertTrue(text.startswith("TITLE: "))
        self.assertIn("FCM: NON-DROP FRAME", text)

    def test_the_events_run_end_to_end_on_the_record_side(self):
        """A gap in the record timecode is black in the conformed sequence."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=50.0, source_end_sec=53.0)])
        rows = [line for line in avid.to_edl(model, model["sequences"][0], 30).splitlines()
                if line[:3].isdigit()]
        self.assertEqual(rows[0].split()[-2:], ["00:00:00:00", "00:00:02:00"])
        self.assertEqual(rows[1].split()[-2:], ["00:00:02:00", "00:00:05:00"])

    def test_the_title_can_be_the_operators(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        text = avid.to_edl(model, model["sequences"][0], 30, title="EP 12 rough")
        self.assertTrue(text.startswith("TITLE: EP 12 rough"))


class RefusalTests(unittest.TestCase):
    def test_a_rate_with_no_honest_timecode_is_refused(self):
        """Writing 31.7 as timecode would invent frames that do not exist."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        with self.assertRaises(ModelError) as raised:
            avid.to_edl(model, model["sequences"][0], 31.7)
        self.assertIn("no non-drop-frame timecode", str(raised.exception))

    def test_the_same_rates_the_exporter_accepts_are_accepted_here(self):
        """A rate one writes and the other refuses is a difference nobody sees
        until the export they were told to compare fails."""
        for fps in (23.976, 24, 25, 29.97, 30, 50, 59.94, 60):
            with self.subTest(fps=fps):
                self.assertEqual(avid.tc_base(fps), exchange._tc_base(fps))

    def test_a_model_with_no_rate_asks_rather_than_picking_one(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        model["sequences"][0]["fps"] = None
        with self.assertRaises(ModelError) as raised:
            avid.sequence_fps(model["sequences"][0])
        self.assertIn("--fps", str(raised.exception))

    def test_a_sequence_of_nothing_but_slivers_is_an_error_not_an_empty_edl(self):
        model = _model([Cut(sequence_order=1, source_start_sec=5.0, source_end_sec=5.001)])
        with self.assertRaises(ModelError) as raised:
            avid.to_edl(model, model["sequences"][0], 30)
        self.assertIn("shorter than one frame", str(raised.exception))


class WhatTheFormatCannotCarryTests(unittest.TestCase):
    """An EDL is cuts. Everything else the model asks for is named, not dropped."""

    def _write(self, model, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                avid.write(model, model["sequences"][0], str(Path(tmp) / "seq.edl"), **kw)
            return out.getvalue()

    def test_it_says_the_edl_names_a_reel_and_not_a_file(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        self.assertIn("reel STREAM", self._write(model))

    def test_the_captions_the_music_and_the_effects_are_named(self):
        model = _model(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0,
                 visual_effect={"type": "zoom", "scale": 1.2})],
            subtitles=[SubtitleLine(0.0, 2.0, "first")],
            structure={"bgm": {"path": "/music/bed.mp3"}},
        )
        said = self._write(model)
        self.assertIn("1 caption(s)", said)
        self.assertIn("audio placement(s)", said)
        self.assertIn("effect(s)/transition(s)", said)

    def test_a_span_shorter_than_a_frame_is_named(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=5.0, source_end_sec=5.001)])
        self.assertIn("shorter than one frame", self._write(model))

    def test_the_file_is_written_where_it_was_asked_for(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "seq.edl"
            with contextlib.redirect_stdout(io.StringIO()):
                avid.write(model, model["sequences"][0], str(target))
            self.assertTrue(target.exists())
            self.assertIn("FCM: NON-DROP FRAME", target.read_text(encoding="utf-8"))


class TheButtonTests(unittest.TestCase):
    """4장 through an adapter with no editor to press it in."""

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
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(out):
                written = avid.build_from_engine(
                    "/broadcasts/stream.mkv", out_dir=tmp, poll_sec=0,
                    engine=engine, **kwargs,
                )
            return written, out.getvalue()

    def test_it_writes_one_edl_per_episode(self):
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
        engine = self.FakeEngine([], states=[{
            "running": False, "state": "FAILED", "error": "",
            "report": {"error": "whisperx is not installed"},
        }])

        with self.assertRaises(EngineError) as raised:
            self._run(engine)

        self.assertIn("whisperx is not installed", str(raised.exception))

    def test_the_persons_mode_reaches_the_engine(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        engine = self.FakeEngine([{"episode_id": "e1"}], model=model)

        self._run(engine, mode="edit_current")

        self.assertEqual(engine.asked_modes, ["edit_current"])


class HonestyTests(unittest.TestCase):
    def test_it_says_the_import_was_never_run(self):
        source = (PLUGIN / "avid" / "aicut_avid.py").read_text(encoding="utf-8")
        self.assertIn("NOT VERIFIED IN MEDIA COMPOSER", source)

    def test_it_says_why_it_writes_an_edl_and_not_an_aaf(self):
        """AAF carries more; it also needs a library this cannot assume."""
        source = (PLUGIN / "avid" / "aicut_avid.py").read_text(encoding="utf-8")
        self.assertIn("AAF", source)


if __name__ == "__main__":
    unittest.main()
