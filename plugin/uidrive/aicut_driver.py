"""Pressing the keys, in the editor that is already open (2장, 5장).

`aicut_steps.py` decides what to do; this carries it out. The split is the same
one the other adapters have: the decisions are testable on a machine with no
editor installed, and the part that touches somebody else's application is
small enough to read in one sitting.

Three ways to carry a step out:

    DryRun      print it and press nothing. Always available, and what --dry-run
                uses, because the first thing to check is the list itself.
    X11         Linux, through XTEST - the same interface a keyboard driver
                uses, so the application cannot tell the difference.
    Windows     ctypes into user32: FindWindow to bring the editor forward,
                SendInput to type.
    macOS       System Events through osascript, which is how a Mac scripts an
                application that does not script itself.

Standard library only. XTEST and user32 are reached through ctypes; the Mac one
shells out to osascript, which every Mac has.
"""

import ctypes
import ctypes.util
import platform
import subprocess
import sys
import time

from aicut_steps import KEYS, MENU, TYPE, WAIT


class DriverError(Exception):
    """The editor could not be found, or could not be typed into."""


class Driver(object):
    """What every driver can do. A step is one of four things and no more."""

    name = "driver"

    def focus(self, window_names):
        """Bring the editor forward. 5장 1: 현재 편집기 프로젝트 확인 starts here -
        a program that types into whatever happens to be in front is a program
        that types into the wrong application."""
        raise NotImplementedError

    def press(self, chord):
        raise NotImplementedError

    def write(self, text):
        raise NotImplementedError

    def menu(self, path):
        raise NotImplementedError

    def run(self, steps, on_step=None):
        """Carry out the whole list, saying what it is doing as it goes (26장)."""
        for index, step in enumerate(steps, start=1):
            if on_step:
                on_step(index, len(steps), step)
            if step.kind == WAIT:
                time.sleep(step.seconds or 0.0)
                continue
            if step.kind == KEYS:
                self.press(step.value)
            elif step.kind == TYPE:
                self.write(step.value)
            elif step.kind == MENU:
                self.menu(step.value)
            else:
                raise DriverError("unknown step kind {!r}".format(step.kind))
            if step.seconds:
                time.sleep(step.seconds)
        return len(steps)


class DryRun(Driver):
    """Presses nothing. The list is printed and the editor is not touched."""

    name = "dry-run"

    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.done = []

    def focus(self, window_names):
        self.out.write("would bring forward: {}\n".format(" / ".join(window_names)))
        return True

    def press(self, chord):
        self.done.append((KEYS, chord))

    def write(self, text):
        self.done.append((TYPE, text))

    def menu(self, path):
        self.done.append((MENU, tuple(path)))


# -- Linux ------------------------------------------------------------------
#: The X keysyms this needs by name. XStringToKeysym answers for the rest.
_X_NAMED = {
    "return": "Return", "enter": "Return", "escape": "Escape", "tab": "Tab",
    "space": "space", "backspace": "BackSpace", "delete": "Delete",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End",
}

#: The characters X names rather than takes literally. Measured, not guessed:
#: `XStringToKeysym(":")` returns nothing, and the first run of this driver
#: stopped on a timecode because of it.
_X_PUNCTUATION = {
    ":": "colon", ";": "semicolon", ".": "period", ",": "comma",
    "-": "minus", "_": "underscore", "+": "plus", "=": "equal",
    "/": "slash", "\\": "backslash", "|": "bar", "?": "question",
    "!": "exclam", "@": "at", "#": "numbersign", "$": "dollar",
    "%": "percent", "^": "asciicircum", "&": "ampersand", "*": "asterisk",
    "(": "parenleft", ")": "parenright", "[": "bracketleft", "]": "bracketright",
    "{": "braceleft", "}": "braceright", "<": "less", ">": "greater",
    "'": "apostrophe", "\"": "quotedbl", "`": "grave", "~": "asciitilde",
    " ": "space",
}

#: Modifier names as a person writes them in a keymap, and the keysym each is.
_X_MODIFIERS = {
    "ctrl": "Control_L", "control": "Control_L",
    "shift": "Shift_L", "alt": "Alt_L", "meta": "Super_L",
    "cmd": "Super_L", "super": "Super_L",
}


class X11Driver(Driver):
    """Linux, through XTEST.

    XTEST is what a testing tool uses to press a key at the X server rather than
    at one window, so the key arrives exactly as a keyboard's would - which is
    the point: the editor is being worked, not spoofed.
    """

    name = "x11"

    def __init__(self, display=None):
        x11 = ctypes.util.find_library("X11")
        xtst = ctypes.util.find_library("Xtst")
        if not x11 or not xtst:
            raise DriverError(
                "this machine has no X11/XTEST libraries, so keys cannot be sent. "
                "Install libxtst6 (or run the editor on a machine that has it)."
            )
        self.x = ctypes.cdll.LoadLibrary(x11)
        self.tst = ctypes.cdll.LoadLibrary(xtst)
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XStringToKeysym.restype = ctypes.c_ulong
        self.x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x.XKeycodeToKeysym.restype = ctypes.c_ulong
        self.x.XKeycodeToKeysym.argtypes = [ctypes.c_void_p, ctypes.c_ubyte,
                                            ctypes.c_int]
        self.display = self.x.XOpenDisplay(
            display.encode("utf-8") if display else None
        )
        if not self.display:
            raise DriverError(
                "no X display. This drives an editor that is on screen, so it "
                "has to run in the session the editor is running in."
            )

    def _keycode(self, name):
        """The key to press for this character, and whether shift is held.

        A colon is not a key: it is shift and the semicolon key, and which key
        that is depends on the layout. Asking X which keysym the key produces
        unshifted is how a person's hand knows to hold shift.
        """
        if len(name) == 1 and name in _X_PUNCTUATION:
            keysym_name = _X_PUNCTUATION[name]
        else:
            keysym_name = _X_NAMED.get(name.lower(), name)
        keysym = self.x.XStringToKeysym(keysym_name.encode("utf-8"))
        if not keysym:
            raise DriverError("X has no key called {!r}".format(name))
        code = self.x.XKeysymToKeycode(self.display, ctypes.c_ulong(keysym))
        if not code:
            raise DriverError("this keyboard layout has no key for {!r}".format(name))
        unshifted = self.x.XKeycodeToKeysym(self.display, ctypes.c_ubyte(code), 0)
        return code, unshifted != keysym

    def _tap(self, code, down=True):
        self.tst.XTestFakeKeyEvent(self.display, ctypes.c_uint(code),
                                   ctypes.c_int(1 if down else 0), ctypes.c_ulong(0))
        self.x.XFlush(self.display)

    def press(self, chord):
        # A chord is "ctrl+shift+s": everything before the last + is held.
        # A lone "+" is the key itself, which is why the split keeps empties out.
        parts = [p for p in str(chord).split("+") if p] or ["+"]
        unknown = [p for p in parts[:-1] if p.lower() not in _X_MODIFIERS]
        if unknown:
            raise DriverError("unknown modifier(s) {} in {!r}".format(unknown, chord))
        modifiers = [self._keycode(_X_MODIFIERS[p.lower()])[0] for p in parts[:-1]]
        key, needs_shift = self._keycode(parts[-1])
        if needs_shift:
            modifiers = [self._keycode(_X_MODIFIERS["shift"])[0]] + modifiers
        for code in modifiers:
            self._tap(code, True)
        self._tap(key, True)
        self._tap(key, False)
        for code in reversed(modifiers):
            self._tap(code, False)

    def write(self, text):
        for character in str(text):
            self.press(character)

    def focus(self, window_names):
        """Bring the editor's window forward, when the session has a way to.

        Without wmctrl or xdotool there is no portable way to raise a window
        from outside a window manager, so this says so rather than typing into
        whatever happens to have focus.
        """
        for tool in ("wmctrl", "xdotool"):
            if _which(tool):
                for name in window_names:
                    if _raise_window(tool, name):
                        return True
        raise DriverError(
            "cannot bring {} forward: neither wmctrl nor xdotool is installed. "
            "Click the editor yourself and run again with --no-focus.".format(
                " / ".join(window_names))
        )

    def menu(self, path):
        raise DriverError(
            "menus are not driven on X11; every step this uses is a key. "
            "The path asked for was {}.".format(" > ".join(path))
        )


def _which(program):
    from shutil import which

    return which(program)


def _raise_window(tool, name):
    try:
        if tool == "wmctrl":
            subprocess.check_call(["wmctrl", "-a", name],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.check_call(["xdotool", "search", "--name", name,
                                   "windowactivate", "--sync"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


# -- Windows ----------------------------------------------------------------
class WindowsDriver(Driver):
    """Windows, through user32: FindWindow to raise the editor, SendInput to type.

    NOT RUN ON WINDOWS YET. This is written against the documented user32 calls
    and has not been executed on a Windows machine with an editor open.
    """

    name = "windows"

    def __init__(self):
        if platform.system() != "Windows":       # pragma: no cover - platform gate
            raise DriverError("this driver is for Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)   # pragma: no cover

    def focus(self, window_names):               # pragma: no cover - needs Windows
        found = []

        def each(handle, _param):
            length = self.user32.GetWindowTextLengthW(handle)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                self.user32.GetWindowTextW(handle, buffer, length + 1)
                title = buffer.value
                for name in window_names:
                    if name.lower() in title.lower():
                        found.append(handle)
            return True

        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        self.user32.EnumWindows(proto(each), 0)
        if not found:
            raise DriverError(
                "no window called {} is open. Start the editor and put the "
                "broadcast on its timeline first (2장).".format(" / ".join(window_names))
            )
        self.user32.SetForegroundWindow(found[0])
        time.sleep(0.3)
        return True

    def press(self, chord):                      # pragma: no cover - needs Windows
        from aicut_win_keys import send_chord

        send_chord(self.user32, chord)

    def write(self, text):                       # pragma: no cover - needs Windows
        from aicut_win_keys import send_text

        send_text(self.user32, text)

    def menu(self, path):                        # pragma: no cover - needs Windows
        raise DriverError("menus are not driven on Windows; every step is a key")


# -- macOS ------------------------------------------------------------------
class MacDriver(Driver):
    """macOS, through System Events - how a Mac scripts an app that does not
    script itself.

    NOT RUN ON MACOS YET. Written against osascript's documented `keystroke`
    and `key code`, and not executed against Final Cut.
    """

    name = "macos"

    #: The keys System Events names rather than types.
    KEY_CODES = {"return": 36, "enter": 36, "escape": 53, "tab": 48,
                 "space": 49, "delete": 51, "up": 126, "down": 125,
                 "left": 123, "right": 124}
    MODIFIERS = {"cmd": "command down", "command": "command down",
                 "ctrl": "control down", "control": "control down",
                 "shift": "shift down", "alt": "option down",
                 "option": "option down"}

    def __init__(self):
        if platform.system() != "Darwin":        # pragma: no cover - platform gate
            raise DriverError("this driver is for macOS")

    def _osascript(self, script):                # pragma: no cover - needs macOS
        try:
            subprocess.check_call(["osascript", "-e", script])
        except (OSError, subprocess.CalledProcessError) as exc:
            raise DriverError(
                "System Events refused the command ({}). Give this program "
                "Accessibility permission in System Settings > Privacy.".format(exc)
            )

    def focus(self, window_names):               # pragma: no cover - needs macOS
        for name in window_names:
            try:
                self._osascript('tell application "{}" to activate'.format(name))
                return True
            except DriverError:
                continue
        raise DriverError("none of {} is running".format(" / ".join(window_names)))

    def press(self, chord):                      # pragma: no cover - needs macOS
        parts = [p for p in str(chord).split("+") if p]
        key = parts[-1].lower()
        modifiers = [self.MODIFIERS[p.lower()] for p in parts[:-1]
                     if p.lower() in self.MODIFIERS]
        using = " using {{{}}}".format(", ".join(modifiers)) if modifiers else ""
        if key in self.KEY_CODES:
            self._osascript('tell application "System Events" to key code {}{}'.format(
                self.KEY_CODES[key], using))
        else:
            self._osascript('tell application "System Events" to keystroke "{}"{}'.format(
                key, using))

    def write(self, text):                       # pragma: no cover - needs macOS
        escaped = str(text).replace("\\", "\\\\").replace('"', '\\"')
        self._osascript(
            'tell application "System Events" to keystroke "{}"'.format(escaped))

    def menu(self, path):                        # pragma: no cover - needs macOS
        raise DriverError("menus are not driven here; every step is a key")


def for_this_machine(dry_run=False, out=None):
    """The driver this computer can use, or the dry run."""
    if dry_run:
        return DryRun(out=out)
    system = platform.system()
    if system == "Windows":
        return WindowsDriver()
    if system == "Darwin":
        return MacDriver()
    if system == "Linux":
        return X11Driver()
    raise DriverError("no driver for {}".format(system))
