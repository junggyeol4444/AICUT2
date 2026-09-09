"""Typing on Windows, through user32's SendInput.

Kept apart from `aicut_driver.py` so the structures below - which only mean
anything on Windows - do not have to be defined on a machine that will never
use them, and so the driver file stays readable.

NOT RUN ON WINDOWS YET. Written against the documented SendInput and
VkKeyScanW; the key names are translated the same way a keyboard layout does,
so a Korean or German layout types the same characters a person would get.
"""

import ctypes

#: SendInput's own constants.
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

#: Virtual-key codes for the keys a keymap names rather than types.
NAMED = {
    "return": 0x0D, "enter": 0x0D, "escape": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}

#: Modifiers, by the names a keymap writes them in.
MODIFIERS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10,
             "alt": 0x12, "meta": 0x5B, "win": 0x5B, "cmd": 0x5B}


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]


def _key_event(vk, scan, flags):
    return _Input(type=INPUT_KEYBOARD,
                  union=_InputUnion(ki=_KeyboardInput(
                      wVk=vk, wScan=scan, dwFlags=flags, time=0,
                      dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))))


def _send(user32, events):
    array = (_Input * len(events))(*events)
    sent = user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(_Input))
    if sent != len(events):
        raise OSError(ctypes.get_last_error(), "SendInput refused the keystroke")


def send_chord(user32, chord):
    """One chord, e.g. `ctrl+s`, held and released in the order a hand does it."""
    parts = [p for p in str(chord).split("+") if p]
    modifiers = [MODIFIERS[p.lower()] for p in parts[:-1] if p.lower() in MODIFIERS]
    unknown = [p for p in parts[:-1] if p.lower() not in MODIFIERS]
    if unknown:
        raise ValueError("unknown modifier(s) {} in {!r}".format(unknown, chord))

    name = parts[-1].lower()
    events = [_key_event(vk, 0, 0) for vk in modifiers]
    if name in NAMED:
        events.append(_key_event(NAMED[name], 0, 0))
        events.append(_key_event(NAMED[name], 0, KEYEVENTF_KEYUP))
    else:
        # The layout decides which key makes this character, which is what a
        # person's hand does too - hard-coding a US layout types the wrong keys
        # on a Korean or German keyboard.
        code = user32.VkKeyScanW(ord(parts[-1]))
        if code == -1:
            raise ValueError("this keyboard layout cannot type {!r}".format(parts[-1]))
        vk = code & 0xFF
        if (code >> 8) & 1 and MODIFIERS["shift"] not in modifiers:
            events.insert(0, _key_event(MODIFIERS["shift"], 0, 0))
            modifiers = [MODIFIERS["shift"]] + modifiers
        events.append(_key_event(vk, 0, 0))
        events.append(_key_event(vk, 0, KEYEVENTF_KEYUP))
    events.extend(_key_event(vk, 0, KEYEVENTF_KEYUP) for vk in reversed(modifiers))
    _send(user32, events)


def send_text(user32, text):
    """Literal text, sent as characters rather than as keys.

    KEYEVENTF_UNICODE puts the character in directly, so a timecode types the
    same on every layout and a name with Korean in it arrives as itself.
    """
    events = []
    for character in str(text):
        events.append(_key_event(0, ord(character), KEYEVENTF_UNICODE))
        events.append(_key_event(0, ord(character), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    if events:
        _send(user32, events)
