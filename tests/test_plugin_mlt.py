"""The Shotcut/Kdenlive adapter, tested without either editor.

Both are built on MLT and read MLT XML, so one adapter serves both. Neither is
in the environment this was written in, so what is checked here is the document:
that it is well-formed, that every entry lands on the frames the model reader
says survive, that the playlist is contiguous, and that nothing in it points at
something that is not there.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from aicut.models import Cut, SubtitleLine
from aicut.render.editmodel import from_edit_plan
from aicut.render.editplan import EditPlan

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
for path in (PLUGIN / "common", PLUGIN / "mlt"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import aicut_mlt as mlt                       # noqa: E402
from aicut_engine import EngineError          # noqa: E402
from aicut_model import ModelError            # noqa: E402
from aicut_model import frame_ranges          # noqa: E402


def _model(cuts, fps=30, duration=7200.0, **kw):
    plan = EditPlan(episode_id="ep123456", project_id="proj",
                    source_path="/broadcasts/stream.mkv", cuts=cuts,
                    render_settings={"fps": fps, "width": 1920, "height": 1080}, **kw)
    return json.loads(json.dumps(
        from_edit_plan(plan, source_duration_sec=duration, fps=fps).as_dict()
    ))


def _root(text):
    return ElementTree.fromstring(text)


def _entries(root):
    playlist = [p for p in root.iter("playlist") if p.get("id") == "playlist0"][0]
    return [(int(e.get("in")), int(e.get("out"))) for e in playlist.iter("entry")]


class TheTimelineTests(unittest.TestCase):
    def test_every_entry_is_a_span_the_model_reader_kept(self):
        """One rule, read once: MLT's `out` is the last frame, like Resolve's."""
        model = _model([
            Cut(sequence_order=1, source_start_sec=100.0, source_end_sec=110.0),
            Cut(sequence_order=2, source_start_sec=300.0, source_end_sec=320.0,
                remove_spans=[[305.0, 307.0]]),
        ])
        sequence = model["sequences"][0]
        text = mlt.to_mlt(model, sequence, 30)
        self.assertEqual(_entries(_root(text)),
                         [(start, end) for start, end, _clip in frame_ranges(sequence, 30)])

    def test_timeline_order_is_kept_not_source_order(self):
        """2.4: a video may open on the moment that happened last."""
        model = _model([Cut(sequence_order=1, source_start_sec=500.0, source_end_sec=505.0),
                        Cut(sequence_order=2, source_start_sec=10.0, source_end_sec=15.0)])
        entries = _entries(_root(mlt.to_mlt(model, model["sequences"][0], 30)))
        self.assertEqual([start for start, _end in entries], [15000, 300])

    def test_the_playlist_is_the_length_of_what_is_in_it(self):
        """The tractor states the timeline's length; a wrong one leaves black at
        the end or cuts the last clip off in the editor's timeline."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=50.0, source_end_sec=53.0)])
        root = _root(mlt.to_mlt(model, model["sequences"][0], 30))
        frames = sum(end - start + 1 for start, end in _entries(root))
        tractor = list(root.iter("tractor"))[0]
        self.assertEqual(int(tractor.get("out")), frames - 1)
        self.assertEqual(frames, 150)

    def test_a_removal_inside_a_cut_becomes_two_entries(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=10.0,
                            remove_spans=[[4.0, 6.0]])])
        self.assertEqual(len(_entries(_root(mlt.to_mlt(model, model["sequences"][0], 30)))), 2)

    def test_an_ntsc_rate_is_a_fraction_not_a_decimal(self):
        """29.97 drifts a frame every thousand; 30000/1001 does not."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)],
                       fps=29.97)
        profile = list(_root(mlt.to_mlt(model, model["sequences"][0], 29.97)).iter("profile"))[0]
        self.assertEqual(profile.get("frame_rate_num"), "30000")
        self.assertEqual(profile.get("frame_rate_den"), "1001")

    def test_the_source_is_the_path_the_model_states(self):
        """MLT relinks by resource; resolving it here against this machine is how
        a Linux path became a drive letter in the Final Cut adapter."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        text = mlt.to_mlt(model, model["sequences"][0], 30)
        resources = [p.text for p in _root(text).iter("property")
                     if p.get("name") == "resource"]
        self.assertIn("/broadcasts/stream.mkv", resources)


class TheDocumentIsConsistentTests(unittest.TestCase):
    """A file that opens is not the same as a file that is right, but a file
    that points at something missing does not open at all."""

    def setUp(self):
        model = _model([Cut(sequence_order=1, source_start_sec=100.0, source_end_sec=110.0),
                        Cut(sequence_order=2, source_start_sec=300.0, source_end_sec=320.0)])
        self.root = _root(mlt.to_mlt(model, model["sequences"][0], 30))
        self.ids = {node.get("id") for node in list(self.root) if node.get("id")}

    def test_every_entry_names_a_producer_that_exists(self):
        for entry in self.root.iter("entry"):
            self.assertIn(entry.get("producer"), self.ids)

    def test_every_track_names_something_that_exists(self):
        tracks = [t.get("producer") for t in self.root.iter("track")]
        self.assertTrue(tracks)
        for producer in tracks:
            self.assertIn(producer, self.ids)

    def test_there_is_a_background_under_the_timeline(self):
        """Both editors write one; without it a timeline has nothing to sit on."""
        self.assertIn("background", self.ids)

    def test_it_declares_its_encoding(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        self.assertTrue(mlt.to_mlt(model, model["sequences"][0], 30).startswith(
            '<?xml version="1.0" encoding="utf-8"?>'))


class RefusalTests(unittest.TestCase):
    def test_a_rate_mlt_cannot_state_exactly_is_refused(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        with self.assertRaises(ModelError) as raised:
            mlt.to_mlt(model, model["sequences"][0], 31.7)
        self.assertIn("not a rate MLT can state", str(raised.exception))

    def test_a_model_with_no_rate_asks_rather_than_picking_one(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0)])
        model["sequences"][0]["fps"] = None
        with self.assertRaises(ModelError) as raised:
            mlt.sequence_fps(model["sequences"][0])
        self.assertIn("--fps", str(raised.exception))

    def test_a_sequence_of_nothing_but_slivers_is_an_error_not_an_empty_timeline(self):
        model = _model([Cut(sequence_order=1, source_start_sec=5.0, source_end_sec=5.001)])
        with self.assertRaises(ModelError) as raised:
            mlt.to_mlt(model, model["sequences"][0], 30)
        self.assertIn("shorter than one frame", str(raised.exception))


class WhatTheFormatDoesNotCarryTests(unittest.TestCase):
    def _write(self, model, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                mlt.write(model, model["sequences"][0], str(Path(tmp) / "seq.mlt"), **kw)
            return out.getvalue()

    def test_the_captions_the_music_and_the_markers_are_named(self):
        model = _model(
            [Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)],
            subtitles=[SubtitleLine(0.0, 2.0, "first")],
            structure={"bgm": {"path": "/music/bed.mp3"}},
        )
        said = self._write(model)
        self.assertIn("1 caption(s)", said)
        self.assertIn("audio placement(s)", said)
        self.assertIn("marker(s)", said)

    def test_a_span_shorter_than_a_frame_is_named(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=2.0),
                        Cut(sequence_order=2, source_start_sec=5.0, source_end_sec=5.001)])
        self.assertIn("shorter than one frame", self._write(model))

    def test_the_file_is_written_and_parses(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "seq.mlt"
            with contextlib.redirect_stdout(io.StringIO()):
                mlt.write(model, model["sequences"][0], str(target))
            self.assertTrue(target.exists())
            ElementTree.parse(target)          # raises if it is not well-formed


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
                written = mlt.build_from_engine(
                    "/broadcasts/stream.mkv", out_dir=tmp, poll_sec=0,
                    engine=engine, **kwargs,
                )
            return written, out.getvalue()

    def test_it_writes_one_document_per_episode(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        engine = self.FakeEngine([{"episode_id": "e1"}, {"episode_id": "e2"}], model=model)

        written, _said = self._run(engine)

        self.assertEqual(len(written), 2)

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
    def test_it_says_neither_editor_has_opened_it(self):
        source = (PLUGIN / "mlt" / "aicut_mlt.py").read_text(encoding="utf-8")
        self.assertIn("NOT VERIFIED IN SHOTCUT OR KDENLIVE", source)

    def test_it_states_which_way_its_out_frame_goes(self):
        """One frame either way on every cut is invisible until the export."""
        source = (PLUGIN / "mlt" / "aicut_mlt.py").read_text(encoding="utf-8")
        self.assertIn("OUT_FRAME_IS_INCLUSIVE = True", source)


if __name__ == "__main__":
    unittest.main()
