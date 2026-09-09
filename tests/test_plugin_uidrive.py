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
        """No copy of these editors exists here, so no key can be confirmed.

        Learning one from the program's own source does not confirm it: that is
        the editor's default, not this person's setting. This test is what stops
        a later edit quietly marking one true.
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

    def test_it_says_which_keys_are_guesses_and_which_are_defaults(self):
        """Three states, and the run names two of them before it starts:
        a hand-written guess, and a default that is not this person's setting."""
        model = _model([Cut(sequence_order=1, source_start_sec=0.0, source_end_sec=5.0)])
        out = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(out):
            uidrive.edit(model, model["sequences"][0], "shotcut", dry_run=True)
        said = out.getvalue()
        self.assertIn("hand-written guesses", said)
        self.assertIn("goto_timecode", said)
        self.assertIn("defaults rather than your settings", said)

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


class LearningTheKeysTests(unittest.TestCase):
    """Finding the keys out rather than being told them.

    The fixtures are excerpts of the editors' own source files, kept here so
    this runs without a network. The keys they yield were checked against the
    full files when they were fetched.
    """

    FIXTURES = Path(__file__).resolve().parent / "fixtures" / "uidrive"

    def _read(self, name):
        return (self.FIXTURES / name).read_text(encoding="utf-8")

    def test_shotcuts_own_source_says_which_keys_mark_in_and_out(self):
        import aicut_learn as learn

        found = learn.from_qt_source(self._read("shotcut_player.cpp"),
                                     ["playerSetInAction", "playerSetOutAction"])
        self.assertEqual(found, {"playerSetInAction": "i", "playerSetOutAction": "o"})

    def test_shotcuts_own_source_says_which_key_appends(self):
        import aicut_learn as learn

        found = learn.from_qt_source(self._read("shotcut_timelinedock.cpp"),
                                     ["timelineAppendAction"])
        self.assertEqual(found, {"timelineAppendAction": "a"})

    def test_a_qt_designer_file_is_read_too(self):
        import aicut_learn as learn

        found = learn.from_qt_ui(self._read("shotcut_mainwindow.ui"),
                                 ["actionSave", "actionNew"])
        self.assertEqual(found, {"actionSave": "ctrl+s", "actionNew": "ctrl+n"})

    def test_kdenlives_own_source_says_the_same_two_keys(self):
        import aicut_learn as learn

        found = learn.from_qt_source(self._read("kdenlive_monitormanager.cpp"),
                                     ["mark_in", "mark_out", "seek_zone_start"])
        self.assertEqual(found, {"mark_in": "i", "mark_out": "o",
                                 "seek_zone_start": "shift+i"})

    def test_a_qt_modifier_expression_becomes_a_chord(self):
        import aicut_learn as learn

        self.assertEqual(learn.qt_chord("Qt::CTRL | Qt::Key_I"), "ctrl+i")
        self.assertEqual(learn.qt_chord("Qt::SHIFT | Qt::ALT | Qt::Key_Return"),
                         "shift+alt+return")
        self.assertIsNone(learn.qt_chord("QKeySequence()"))

    def test_an_action_with_no_shortcut_is_not_given_one(self):
        """Qt says "no default" by assigning nothing; inventing one here would
        press a key the editor has not bound."""
        import aicut_learn as learn

        source = 'addAction(QStringLiteral("no_key"), thing, QKeySequence());'
        self.assertEqual(learn.from_qt_source(source, ["no_key"]), {})

    def test_a_persons_own_kde_override_is_read_and_wins(self):
        """A shortcut they changed is the shortcut the editor obeys."""
        import aicut_learn as learn

        text = "[Shortcuts]\nmark_in=Ctrl+Shift+I\t\nmark_out=none\n"
        self.assertEqual(learn.from_kde_shortcuts(text, ["mark_in", "mark_out"]),
                         {"mark_in": "ctrl+shift+i"})

    def test_final_cuts_command_set_is_read_as_a_plist(self):
        import plistlib

        import aicut_learn as learn

        data = plistlib.dumps({
            "MarkIn": {"characterString": "i", "modifiers": ""},
            "AppendToStoryline": {"characterString": "e", "modifiers": "command"},
            "Odd": {"nothing": 1},
        })
        found, unreadable = learn.from_final_cut_commandset(
            data, ["MarkIn", "AppendToStoryline", "Odd"])
        self.assertEqual(found, {"MarkIn": "i", "AppendToStoryline": "cmd+e"})
        self.assertEqual(unreadable, ["Odd"])

    def test_what_is_learned_carries_where_it_came_from(self):
        import aicut_learn as learn

        pages = {"src/player.cpp": self._read("shotcut_player.cpp")}
        found, notes = learn.learn(
            "shotcut", {"mark_in": "playerSetInAction"},
            fetch=lambda url: pages[url],
            sources=[["src/player.cpp", "qt-source"]],
            home=str(self.FIXTURES),        # nothing of shotcut's is in there
        )
        self.assertEqual(found["mark_in"]["key"], "i")
        self.assertIn("src/player.cpp", found["mark_in"]["source"])
        self.assertEqual(notes, [])

    def test_a_default_read_from_source_is_not_called_confirmed(self):
        """It is what the editor ships with, not what this person set it to."""
        import aicut_learn as learn

        found, _notes = learn.learn(
            "shotcut", {"mark_in": "playerSetInAction"},
            fetch=lambda url: self._read("shotcut_player.cpp"),
            sources=[["anywhere", "qt-source"]], home=str(self.FIXTURES),
        )
        self.assertFalse(found["mark_in"]["confirmed"])

    def test_a_source_that_cannot_be_read_is_reported_not_swallowed(self):
        import aicut_learn as learn

        def refuse(url):
            raise OSError("no network here")

        found, notes = learn.learn(
            "shotcut", {"mark_in": "playerSetInAction"}, fetch=refuse,
            sources=[["https://example.invalid/x.cpp", "qt-source"]],
            home=str(self.FIXTURES),
        )
        self.assertEqual(found, {})
        self.assertTrue(any("no network here" in note for note in notes))

    def test_merging_keeps_what_was_not_learned(self):
        import aicut_learn as learn

        keymap = {"keys": {"mark_in": {"key": "x", "confirmed": False},
                           "confirm": {"key": "return", "confirmed": False}}}
        changed = learn.merge_into_keymap(
            keymap, {"mark_in": {"key": "i", "confirmed": True, "source": "here"}})
        self.assertEqual(keymap["keys"]["mark_in"]["key"], "i")
        self.assertTrue(keymap["keys"]["mark_in"]["confirmed"])
        self.assertEqual(keymap["keys"]["confirm"]["key"], "return")
        self.assertEqual(changed, [("mark_in", "x", "i", "here")])

    def test_the_shipped_keymap_says_where_each_key_came_from(self):
        """The two open editors' keys were learned from their own source; the
        rest are still hand-written, and the file says which is which."""
        shotcut = KEYMAPS["shotcut"]["keys"]
        for name in ("mark_in", "mark_out", "append", "save"):
            with self.subTest(key=name):
                self.assertIn("source", shotcut[name])
                self.assertIn("raw.githubusercontent.com", shotcut[name]["source"])
        self.assertEqual(shotcut["mark_in"]["key"], "i")
        self.assertEqual(shotcut["append"]["key"], "a")
        self.assertEqual(KEYMAPS["kdenlive"]["keys"]["append"]["key"], "v")

    def test_the_ones_that_could_not_be_learned_are_still_marked_as_guesses(self):
        self.assertIn("goto_timecode", steps_mod.unsourced(KEYMAPS["shotcut"]))
        self.assertNotIn("mark_in", steps_mod.unsourced(KEYMAPS["shotcut"]))


#: A real window, made with the same user32 calls an application uses, so
#: SendInput has something to deliver to. Test-only: the plugin drives editors
#: that already exist, and this is the editor's stand-in on Windows.
WINDOWS_RECEIVER = r"""
import ctypes, ctypes.wintypes as w, json, sys, time

user32 = ctypes.WinDLL("user32", use_last_error=True)
WM_KEYDOWN, WM_CHAR, WM_QUIT = 0x0100, 0x0102, 0x0012
got = []

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, w.HWND, ctypes.c_uint,
                             ctypes.c_ulonglong, ctypes.c_longlong)

def proc(hwnd, message, wparam, lparam):
    if message in (WM_KEYDOWN, WM_CHAR):
        got.append({"message": message, "wparam": int(wparam),
                    "ctrl": bool(user32.GetKeyState(0x11) & 0x8000),
                    "shift": bool(user32.GetKeyState(0x10) & 0x8000)})
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)

class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]

callback = WNDPROC(proc)
cls = WNDCLASS()
cls.lpfnWndProc = callback
cls.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
cls.lpszClassName = "AicutStandIn"
if not user32.RegisterClassW(ctypes.byref(cls)):
    print("NOWINDOW register", flush=True); sys.exit(0)

user32.CreateWindowExW.restype = w.HWND
hwnd = user32.CreateWindowExW(0, "AicutStandIn", "Shotcut", 0x00CF0000,
                              10, 10, 300, 200, None, None, cls.hInstance, None)
if not hwnd:
    print("NOWINDOW create", flush=True); sys.exit(0)
user32.ShowWindow(hwnd, 5)
user32.SetForegroundWindow(hwnd)
user32.SetFocus(hwnd)
print("FOREGROUND" if user32.GetForegroundWindow() == hwnd else "NOFOCUS", flush=True)

wanted = int(sys.argv[2])
message = w.MSG()
deadline = time.time() + 20
while time.time() < deadline and len(got) < wanted:
    while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    time.sleep(0.01)
json.dump(got, open(sys.argv[1], "w"))
print("DONE", len(got), flush=True)
"""


@unittest.skipUnless(sys.platform == "win32", "the Windows driver needs Windows")
class TheWindowsDriverReallyTypesTests(unittest.TestCase):
    """The Windows driver against a real window, on a real Windows machine.

    The same shape as the X11 test: a window is created with the user32 calls an
    application uses, the driver sends keys with SendInput, and the window
    reports what its message loop received. It runs where Windows is - the CI
    matrix has a windows-latest job - and nowhere else.
    """

    def _received(self, presses, expected):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "receiver.py"
            script.write_text(WINDOWS_RECEIVER, encoding="utf-8")
            target = Path(tmp) / "got.json"
            with subprocess.Popen(
                [sys.executable, str(script), str(target), str(expected)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ) as receiver:
                first = receiver.stdout.readline().strip()
                if first.startswith("NOWINDOW"):
                    receiver.wait(timeout=10)
                    self.skipTest("this Windows session cannot create a window ({})"
                                  .format(first))
                if first == "NOFOCUS":
                    # Nothing can be delivered to a window that is not in front,
                    # and that is the session's doing rather than the driver's.
                    receiver.kill()
                    self.skipTest("this Windows session would not bring the "
                                  "window to the foreground")
                time.sleep(0.5)
                driver = driver_mod.WindowsDriver()
                presses(driver)
                receiver.wait(timeout=25)
            return json.loads(target.read_text(encoding="utf-8"))

    def test_a_timecode_arrives_as_a_timecode(self):
        got = self._received(lambda d: d.write("00:01:40:00"), 11)
        typed = "".join(chr(row["wparam"]) for row in got if row["message"] == 0x0102)
        self.assertEqual(typed, "00:01:40:00")

    def test_a_chord_arrives_with_its_modifier_held(self):
        got = self._received(lambda d: d.press("ctrl+s"), 1)
        self.assertTrue(got, "nothing arrived")
        self.assertTrue(any(row["ctrl"] for row in got), "ctrl was not held")

    def test_the_marks_a_cut_needs_arrive_as_themselves(self):
        got = self._received(lambda d: (d.press("i"), d.press("o")), 2)
        keys = [chr(row["wparam"]).lower() for row in got
                if row["message"] == 0x0100]
        self.assertEqual(keys[:2], ["i", "o"])


class TheMacCommandTests(unittest.TestCase):
    """What the Mac driver says to System Events, checked without a Mac.

    The part that needs macOS is running the script; writing it does not, so it
    is a function of its own and this reads what it writes.
    """

    def test_a_chord_becomes_a_keystroke_with_its_modifier(self):
        self.assertEqual(
            driver_mod.mac_command("cmd+s"),
            'tell application "System Events" to keystroke "s" using {command down}')

    def test_a_named_key_becomes_a_key_code(self):
        """`keystroke "return"` types the word; the key is a number there."""
        self.assertEqual(driver_mod.mac_command("return"),
                         'tell application "System Events" to key code 36')

    def test_two_modifiers_are_both_named(self):
        self.assertIn("{command down, shift down}", driver_mod.mac_command("cmd+shift+n"))

    def test_a_timecode_is_typed_as_itself(self):
        self.assertEqual(
            driver_mod.mac_type_command("00:01:40:00"),
            'tell application "System Events" to keystroke "00:01:40:00"')

    def test_a_quote_in_the_text_does_not_end_the_script(self):
        """A sequence name is the model's, and AppleScript is a string here."""
        self.assertEqual(driver_mod.mac_type_command('AI "best of"'),
                         'tell application "System Events" to keystroke '
                         '"AI \\"best of\\""')

    def test_an_unknown_modifier_is_refused_rather_than_dropped(self):
        with self.assertRaises(driver_mod.DriverError):
            driver_mod.mac_command("hyper+s")


@unittest.skipUnless(sys.platform == "darwin", "osascript is macOS's")
class TheMacScriptIsAcceptedTests(unittest.TestCase):
    """osascript reading the commands, on a real Mac.

    System Events refuses to type without Accessibility permission, which CI
    does not grant - so what is checked here is that the script itself is
    accepted: a syntax error and a permission error are different answers, and
    only the second is the machine's rather than this program's.
    """

    def _compile(self, script):
        return subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=30)

    def test_the_commands_are_syntactically_accepted(self):
        for script in (driver_mod.mac_command("cmd+s"),
                       driver_mod.mac_command("return"),
                       driver_mod.mac_type_command("00:01:40:00")):
            with self.subTest(script=script):
                done = self._compile(script)
                self.assertNotIn("syntax error", done.stderr.lower(), done.stderr)
                self.assertNotIn("expected", done.stderr.lower(), done.stderr)


class HonestyTests(unittest.TestCase):
    def test_the_driver_says_which_platforms_it_has_run_on(self):
        source = (UIDRIVE / "aicut_driver.py").read_text(encoding="utf-8")
        self.assertIn("NOT RUN ON WINDOWS YET", source)
        self.assertIn("NOT RUN ON MACOS YET", source)

    def test_the_entry_point_says_no_editor_has_seen_it(self):
        source = (UIDRIVE / "aicut_uidrive.py").read_text(encoding="utf-8")
        self.assertIn("NOT RUN AGAINST ANY OF THE FOUR EDITORS", source)

    def test_the_keymap_file_says_what_confirmed_means_in_it(self):
        """Three states, and the file has to distinguish them or the word
        `confirmed` quietly comes to mean "somebody wrote it down"."""
        note = " ".join(KEYMAPS["_note"])
        self.assertIn("true only where the key was read from the copy", note)
        self.assertIn("not what this person set it to", note)
        self.assertIn("written down by hand", note)


if __name__ == "__main__":
    unittest.main()
