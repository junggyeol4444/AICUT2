"""Working an editor that has no plugin, the way a person does (2장, 5장).

Resolve, Premiere and VEGAS let a plugin call them. Final Cut, Avid, Shotcut
and Kdenlive do not, and 2장 still gives the person four steps and no more - so
those four are worked through their own window: the program brings the editor
forward and types.

None of the four is installed here, so what is tested is what can be: the list
of steps, the keymap's honesty about itself, and - on Linux, against a real X
client that records what arrives - the driver that presses the keys.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aicut.models import Cut, SubtitleLine
from aicut.render.editmodel import from_edit_plan
from aicut.render.editplan import EditPlan

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
UIDRIVE = PLUGIN / "uidrive"
for path in (PLUGIN / "common", UIDRIVE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import aicut_driver as driver_mod              # noqa: E402
import aicut_steps as steps_mod                # noqa: E402
import aicut_uidrive as uidrive                # noqa: E402
from aicut_model import ModelError             # noqa: E402

KEYMAPS = json.loads((UIDRIVE / "keymaps.json").read_text(encoding="utf-8"))
EDITORS = sorted(k for k in KEYMAPS if not k.startswith("_"))


def _model(cuts, fps=30, duration=7200.0, **kw):
    plan = EditPlan(episode_id="ep123456", project_id="proj",
                    source_path="/broadcasts/stream.mkv", cuts=cuts,
                    render_settings={"fps": fps}, **kw)
    return json.loads(json.dumps(
        from_edit_plan(plan, source_duration_sec=duration, fps=fps).as_dict()
    ))


class TheStepsTests(unittest.TestCase):
    """What a person does, in order, before anything touches a keyboard."""

    def setUp(self):
        self.keymap = steps_mod.load_keymap("shotcut")

    def _steps(self, cuts, fps=30, **kw):
        model = _model(cuts, fps=fps)
        return steps_mod.build_steps(model, model["sequences"][0], fps,
                                     self.keymap, **kw)

    def test_every_surviving_span_is_marked_and_appended(self):
        """Two cuts, one of them split by a removal, is three marked spans."""
        steps = self._steps([
            Cut(sequence_order=1, source_start_sec=100.0, source_end_sec=110.0),
            Cut(sequence_order=2, source_start_sec=300.0, source_end_sec=320.0,
                remove_spans=[[305.0, 307.0]]),
        ])
        appends = [s for s in steps if s.kind == steps_mod.KEYS
                   and s.value == self.keymap["keys"]["append"]["key"]]
        self.assertEqual(len(appends), 3)

    def test_the_in_and_out_points_are_the_timecodes_of_those_spans(self):
        steps = self._steps([Cut(sequence_order=1, source_start_sec=100.0,
                                 source_end_sec=110.0)])
        typed = [s.value for s in steps if s.kind == steps_mod.TYPE]
        self.assertIn("00:01:40:00", typed)
        # frame_ranges answers with the last frame of the span, so the out point
        # is 9.967s after the in point at 30 fps - not 110.000, which would take
        # one frame of the next thing.
        self.assertIn("00:01:49:29", typed)

    def test_timeline_order_is_kept_not_source_order(self):
        """2.4: a video may open on the moment that happened last."""
        steps = self._steps([
            Cut(sequence_order=1, source_start_sec=500.0, source_end_sec=505.0),
            Cut(sequence_order=2, source_start_sec=10.0, source_end_sec=15.0),
        ])
        typed = [s.value for s in steps if s.kind == steps_mod.TYPE
                 and s.value.count(":") == 3]
        self.assertEqual(typed[0], "00:08:20:00")
        self.assertEqual(typed[2], "00:00:10:00")

    def test_mode_a_makes_a_new_timeline_and_names_it(self):
        """25장 A is the default because the other way edits what they made."""
        steps = self._steps([Cut(sequence_order=1, source_start_sec=0.0,
                                 source_end_sec=5.0)])
        self.assertEqual(steps[1].value, self.keymap["keys"]["new_timeline"]["key"])
        self.assertEqual(steps[2].value, "AI_ep123456")

    def test_mode_b_touches_the_timeline_that_is_open(self):
        steps = self._steps([Cut(sequence_order=1, source_start_sec=0.0,
                                 source_end_sec=5.0)], mode="edit_current")
        keys = [s.value for s in steps if s.kind == steps_mod.KEYS]
        self.assertNotIn(self.keymap["keys"]["new_timeline"]["key"], keys)

    def test_it_saves_at_the_end(self):
        """A six-hour broadcast is a long time to lose to a crash."""
        steps = self._steps([Cut(sequence_order=1, source_start_sec=0.0,
                                 source_end_sec=5.0)])
        self.assertEqual(steps[-1].value, self.keymap["keys"]["save"]["key"])

    def test_a_missing_key_is_refused_by_name(self):
        """Better than pressing something else and hoping."""
        keymap = {"name": "Nothing", "keys": {}}
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        with self.assertRaises(ModelError) as raised:
            steps_mod.build_steps(model, model["sequences"][0], 30, keymap)
        self.assertIn("keymaps.json", str(raised.exception))

    def test_every_step_says_why_it_is_being_done(self):
        """26장 shows these while it runs; a step nobody can explain is a step
        nobody can check."""
        steps = self._steps([Cut(sequence_order=1, source_start_sec=0.0,
                                 source_end_sec=5.0)])
        for step in steps:
            self.assertTrue(step.why, step.kind)

    def test_a_sequence_of_nothing_but_slivers_is_an_error(self):
        model = _model([Cut(sequence_order=1, source_start_sec=5.0, source_end_sec=5.001)])
        with self.assertRaises(ModelError):
            steps_mod.build_steps(model, model["sequences"][0], 30, self.keymap)


class TheKeymapTests(unittest.TestCase):
    """Somebody else's shortcuts, and this file says which it has checked."""

    def test_nothing_claims_to_be_confirmed_that_has_not_been(self):
        """No copy of these editors exists here, so nothing can be confirmed.

        This test is what stops a later edit quietly marking one true.
        """
        for editor in EDITORS:
            with self.subTest(editor=editor):
                self.assertEqual(steps_mod.unconfirmed(KEYMAPS[editor]),
                                 sorted(KEYMAPS[editor]["keys"]))

    def test_every_editor_has_the_keys_an_edit_needs(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        for editor in EDITORS:
            with self.subTest(editor=editor):
                steps_mod.build_steps(model, model["sequences"][0], 30,
                                      steps_mod.load_keymap(editor))

    def test_every_editor_says_which_window_to_look_for(self):
        for editor in EDITORS:
            with self.subTest(editor=editor):
                self.assertTrue(KEYMAPS[editor].get("window"))

    def test_the_four_without_a_plugin_are_the_four_that_are_here(self):
        self.assertEqual(EDITORS, ["avid", "finalcut", "kdenlive", "shotcut"])

    def test_an_unknown_editor_says_which_it_knows(self):
        with self.assertRaises(ModelError) as raised:
            steps_mod.load_keymap("imovie")
        self.assertIn("shotcut", str(raised.exception))


class TheDryRunTests(unittest.TestCase):
    """The first thing to do with this is read what it would do."""

    def test_it_presses_nothing_and_prints_everything(self):
        model = _model([Cut(sequence_order=1, source_start_sec=100.0,
                            source_end_sec=110.0)])
        recorded = driver_mod.DryRun(out=io.StringIO())
        out = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(out):
            steps = uidrive.edit(model, model["sequences"][0], "shotcut",
                                 dry_run=True, driver=recorded)
        printed = out.getvalue()
        self.assertEqual(recorded.done, [])            # nothing was pressed
        self.assertIn("00:01:40:00", printed)
        self.assertIn("{} steps".format(len(steps)), printed)

    def test_it_says_how_many_keys_are_unconfirmed(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        out = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(out):
            uidrive.edit(model, model["sequences"][0], "shotcut", dry_run=True)
        self.assertIn("never been confirmed", out.getvalue())

    def test_a_model_with_no_rate_asks_rather_than_typing_a_guess(self):
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        model["sequences"][0]["fps"] = None
        with self.assertRaises(ModelError) as raised:
            uidrive.edit(model, model["sequences"][0], "shotcut", dry_run=True)
        self.assertIn("--fps", str(raised.exception))


class TheDriverTests(unittest.TestCase):
    def test_the_dry_run_driver_carries_out_every_kind_of_step(self):
        recorded = driver_mod.DryRun(out=io.StringIO())
        steps = [steps_mod.Step(steps_mod.KEYS, "ctrl+s", "save"),
                 steps_mod.Step(steps_mod.TYPE, "00:00:01:00", "a timecode"),
                 steps_mod.Step(steps_mod.WAIT, None, "settle", seconds=0.0)]
        recorded.run(steps)
        self.assertEqual(recorded.done,
                         [(steps_mod.KEYS, "ctrl+s"), (steps_mod.TYPE, "00:00:01:00")])

    def test_an_unknown_step_kind_is_refused(self):
        recorded = driver_mod.DryRun(out=io.StringIO())
        with self.assertRaises(driver_mod.DriverError):
            recorded.run([steps_mod.Step("dance", None, "no")])

    def test_the_progress_of_every_step_is_reported(self):
        """26장's panel is this callback."""
        recorded = driver_mod.DryRun(out=io.StringIO())
        seen = []
        recorded.run([steps_mod.Step(steps_mod.KEYS, "i", "mark in")],
                     on_step=lambda i, n, s: seen.append((i, n, s.why)))
        self.assertEqual(seen, [(1, 1, "mark in")])


def _x11_available():
    if sys.platform != "linux":
        return False
    if not shutil.which("Xvfb"):
        return False
    try:
        import Xlib                            # noqa: F401
    except ImportError:
        return False
    import ctypes.util

    return bool(ctypes.util.find_library("X11") and ctypes.util.find_library("Xtst"))


@unittest.skipUnless(_x11_available(),
                     "needs Xvfb, python-xlib and libXtst to drive a real window")
class TheX11DriverReallyTypesTests(unittest.TestCase):
    """The driver against a real X client, which records what arrived.

    Not a mock: a window is created on a real X server, the driver presses keys
    through XTEST - the same interface a keyboard driver uses - and the window
    reports the keysyms and modifiers it received. It is the only one of the
    three drivers that can be run on this machine, and it is run.
    """

    RECORDER = r'''
import sys, json, time
from Xlib import X, XK, display

d = display.Display()
screen = d.screen()
win = screen.root.create_window(0, 0, 200, 100, 1, screen.root_depth,
                                X.InputOutput, X.CopyFromParent,
                                background_pixel=screen.white_pixel,
                                event_mask=X.KeyPressMask)
win.set_wm_name("Shotcut")
win.map(); d.sync()
win.set_input_focus(X.RevertToParent, X.CurrentTime); d.sync()
print("READY", flush=True)
got = []
wanted = int(sys.argv[2])
deadline = time.time() + 20
while time.time() < deadline and len(got) < wanted:
    if d.pending_events() == 0:
        time.sleep(0.005); continue
    ev = d.next_event()
    if ev.type == X.KeyPress:
        level = 1 if ev.state & X.ShiftMask else 0
        keysym = d.keycode_to_keysym(ev.detail, level)
        got.append({"name": XK.keysym_to_string(keysym),
                    "shift": bool(ev.state & X.ShiftMask),
                    "ctrl": bool(ev.state & X.ControlMask)})
json.dump(got, open(sys.argv[1], "w"))
'''

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.display = ":97"
        cls.xvfb = subprocess.Popen(
            ["Xvfb", cls.display, "-screen", "0", "320x240x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)

    @classmethod
    def tearDownClass(cls):
        cls.xvfb.terminate()
        cls.xvfb.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _typed(self, presses, expected):
        """Run the recorder, press what the caller asks, return what arrived."""
        script = Path(self.tmp) / "recorder.py"
        script.write_text(self.RECORDER, encoding="utf-8")
        target = Path(self.tmp) / "got.json"
        environment = dict(os.environ, DISPLAY=self.display)
        with subprocess.Popen(
            [sys.executable, str(script), str(target), str(expected)],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        ) as recorder:
            self.assertEqual(recorder.stdout.readline().strip(), "READY")

            old = os.environ.get("DISPLAY")
            os.environ["DISPLAY"] = self.display
            try:
                driver = driver_mod.X11Driver()
                presses(driver)
            finally:
                if old is None:
                    os.environ.pop("DISPLAY", None)
                else:
                    os.environ["DISPLAY"] = old
            recorder.wait(timeout=25)
        return json.loads(target.read_text(encoding="utf-8"))

    def test_a_timecode_arrives_as_a_timecode(self):
        """Every colon needs shift held on this layout, and a driver that does
        not hold it types `00;01;40;00` into the editor's field."""
        got = self._typed(lambda d: d.write("00:01:40:00"), 14)
        typed = "".join(row["name"] for row in got if row["name"])
        self.assertEqual(typed.replace(";", ":"), "00:01:40:00")
        colons = [row for row in got if row["name"] in (":", ";")]
        self.assertEqual(len(colons), 3)
        for row in colons:
            self.assertTrue(row["shift"], "the colon arrived without shift")

    def test_a_chord_arrives_with_its_modifier_held(self):
        got = self._typed(lambda d: d.press("ctrl+s"), 2)
        pressed = [row for row in got if row["name"] == "s"]
        self.assertEqual(len(pressed), 1)
        self.assertTrue(pressed[0]["ctrl"], "ctrl was not held")

    def test_the_marks_a_cut_needs_arrive_as_themselves(self):
        got = self._typed(lambda d: (d.press("i"), d.press("o")), 2)
        self.assertEqual([row["name"] for row in got], ["i", "o"])

    def test_a_key_this_layout_does_not_have_is_named_not_guessed(self):
        os.environ["DISPLAY"] = self.display
        try:
            driver = driver_mod.X11Driver()
            with self.assertRaises(driver_mod.DriverError) as raised:
                driver.press("nosuchkey")
            self.assertIn("nosuchkey", str(raised.exception))
        finally:
            os.environ.pop("DISPLAY", None)


class HonestyTests(unittest.TestCase):
    def test_the_driver_says_which_platforms_it_has_run_on(self):
        source = (UIDRIVE / "aicut_driver.py").read_text(encoding="utf-8")
        self.assertIn("NOT RUN ON WINDOWS YET", source)
        self.assertIn("NOT RUN ON MACOS YET", source)

    def test_the_entry_point_says_no_editor_has_seen_it(self):
        source = (UIDRIVE / "aicut_uidrive.py").read_text(encoding="utf-8")
        self.assertIn("NOT RUN AGAINST ANY OF THE FOUR EDITORS", source)

    def test_the_keymap_file_says_nothing_in_it_is_confirmed(self):
        note = " ".join(KEYMAPS["_note"])
        self.assertIn("NOTHING HERE IS CONFIRMED", note)


if __name__ == "__main__":
    unittest.main()
