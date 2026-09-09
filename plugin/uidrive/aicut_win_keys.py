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


#: Windows' own widths, written as fixed-width types rather than as `c_ulong`.
#: `DWORD` is four bytes on Windows; `ctypes.c_ulong` is eight on 64-bit Linux,
#: so a structure declared with it has a different shape depending on where the
#: file is read - and then it cannot be checked anywhere but Windows.
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_LONG = ctypes.c_int32

#: `ULONG_PTR`: 8 bytes where the process is 64-bit, 4 where it is not. Getting
#: this wrong changes the size of every structure below it.
_ULONG_PTR = ctypes.c_void_p


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", _WORD), ("wScan", _WORD),
                ("dwFlags", _DWORD), ("time", _DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class _MouseInput(ctypes.Structure):
    """Not used, and it has to be here.

    `INPUT` is a union of three, and the union is as big as its largest member -
    the mouse one. A union declared with only the keyboard member is 8 bytes
    short on a 64-bit machine, and `SendInput` is told the size: it answered
    ERROR_INVALID_PARAMETER (87) and refused every keystroke. Windows CI caught
    exactly that.
    """

    _fields_ = [("dx", _LONG), ("dy", _LONG),
                ("mouseData", _DWORD), ("dwFlags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ()
    _fields_ = [("type", _DWORD), ("union", _InputUnion)]


#: What Windows expects `sizeof(INPUT)` to be, which is what it is told below.
#: 40 bytes on a 64-bit process, 28 on a 32-bit one.
INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28


def _key_event(vk, scan, flags):
    return _Input(type=INPUT_KEYBOARD,
                  union=_InputUnion(ki=_KeyboardInput(
                      wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)))


def _send(user32, events):
    array = (_Input * len(events))(*events)
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    sent = user32.SendInput(len(events), array, ctypes.sizeof(_Input))
    if sent != len(events):
        raise OSError(ctypes.get_last_error(),
                      "SendInput sent {} of {} events".format(sent, len(events)))


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
