"""Finding out an editor's keys instead of being told them.

`keymaps.json` starts out unconfirmed - nobody here has pressed those keys in
those applications. This module fills it in from the places the answer actually
lives, in the order of how much each is worth:

1. **The copy on this machine.** Final Cut writes its command set as a plist,
   KDE writes its overrides in an ini, and both say what the person's own
   installation is set to - which is the only thing that matters, because a
   shortcut they changed is the shortcut the editor obeys.
2. **The program's own source.** Shotcut and Kdenlive are open, so their
   default keys are stated in the code they are built from - not on a page that
   may be out of date, but in the line that assigns them.

Everything learned carries where it came from, and only what was read from the
installed copy is marked confirmed: a default read from source is what the
editor ships with, not what this person's editor is set to.
"""

import json
import os
import plistlib
import re
import xml.etree.ElementTree as ElementTree

#: Qt names its keys `Qt::Key_A`, and its modifiers `Qt::CTRL | Qt::Key_I`.
#: Both Shotcut and Kdenlive are Qt programs, so one reader does for both.
_QT_MODIFIERS = {"CTRL": "ctrl", "SHIFT": "shift", "ALT": "alt", "META": "meta"}
_QT_NAMED = {
    "Return": "return", "Enter": "return", "Escape": "escape", "Space": "space",
    "Tab": "tab", "Backspace": "backspace", "Delete": "delete",
    "Home": "home", "End": "end", "Up": "up", "Down": "down",
    "Left": "left", "Right": "right",
}
_QT_KEY = re.compile(r"Qt::(?:(CTRL|SHIFT|ALT|META)|Key_([A-Za-z0-9_]+))")


def qt_chord(expression):
    """`Qt::CTRL | Qt::Key_I` as this program writes a chord: `ctrl+i`.

    Returns None when the expression names no key at all, which is how Qt says
    "this action has no default shortcut" - and an action with no shortcut is
    not something to invent one for.
    """
    modifiers = []
    key = None
    for modifier, name in _QT_KEY.findall(expression or ""):
        if modifier:
            modifiers.append(_QT_MODIFIERS[modifier])
        elif name:
            key = _QT_NAMED.get(name, name.lower())
    if not key:
        return None
    return "+".join(modifiers + [key])


def from_qt_source(text, action_names):
    """Default shortcuts out of a Qt program's own source.

    Two shapes, both real and both used by these programs:

        action->setShortcut(QKeySequence(Qt::Key_A));   ... Actions.add("name", action)
        addAction(QStringLiteral("mark_in"), markIn, Qt::Key_I)

    The first is Shotcut's: the shortcut is set on an action that is registered
    under its name a few lines later, so the name is found and the assignment
    before it is the one that belongs to it. The second is Kdenlive's, where
    both are arguments of one call.
    """
    found = {}
    for name in action_names:
        # Kdenlive's shape: the name and the key in one call.
        call = re.search(
            r'addAction\(\s*QStringLiteral\(\s*"%s"\s*\)(.{0,400}?)\)\s*;' % re.escape(name),
            text, re.S,
        )
        if call:
            chord = qt_chord(call.group(1))
            if chord:
                found[name] = chord
                continue
        # Shotcut's shape: registered after the fact, so look back from the name.
        registered = re.search(r'Actions\.add\(\s*"%s"' % re.escape(name), text)
        if registered:
            before = text[:registered.start()]
            shortcut = None
            for match in re.finditer(r"setShortcut\(\s*QKeySequence\(([^;]*?)\)\s*\)", before):
                shortcut = match
            if shortcut:
                chord = qt_chord(shortcut.group(1))
                if chord:
                    found[name] = chord
    return found


def from_qt_ui(text, action_names):
    """Shortcuts out of a Qt Designer .ui file, where they are plain XML."""
    root = ElementTree.fromstring(text)
    found = {}
    for action in root.iter("action"):
        name = action.get("name")
        if name not in action_names:
            continue
        stated = action.find("./property[@name='shortcut']/string")
        if stated is not None and stated.text:
            found[name] = stated.text.strip().lower().replace(" ", "")
    return found


def from_kde_shortcuts(text, action_names):
    """A KDE user's own overrides, from `kdenliveshortcutsrc`.

    KDE writes `action=Ctrl+I; ;` - the shortcut, then the alternate, then the
    default. The first field is what the editor obeys, and an empty one means
    the person removed the shortcut rather than that it is unset.
    """
    found = {}
    for line in text.splitlines():
        if "=" not in line or line.strip().startswith(("#", "[")):
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name not in action_names:
            continue
        primary = value.split("\\t")[0].split(";")[0].strip()
        if primary and primary.lower() not in ("none", "no shortcut"):
            found[name] = primary.lower().replace(" ", "")
    return found


def from_final_cut_commandset(data, action_names):
    """Final Cut's own command set, which is a plist it ships and the person edits.

    Each command is a dictionary with a `modifiers` string and a `key`. The
    shape is read rather than assumed: a file that does not have those fields is
    reported as what it is instead of being guessed at, because a wrong key here
    is a wrong key pressed in somebody's edit.
    """
    plist = plistlib.loads(data) if isinstance(data, bytes) else data
    found = {}
    unreadable = []
    for name in action_names:
        entry = plist.get(name)
        if entry is None:
            continue
        if isinstance(entry, list):
            entry = entry[0] if entry else None
        if not isinstance(entry, dict) or "characterString" not in entry and "key" not in entry:
            unreadable.append(name)
            continue
        key = entry.get("characterString") or entry.get("key") or ""
        modifiers = str(entry.get("modifiers", "")).lower().split()
        parts = [m for m in ("command", "control", "shift", "option") if m in modifiers]
        parts = ["cmd" if p == "command" else "ctrl" if p == "control"
                 else "alt" if p == "option" else p for p in parts]
        if key:
            found[name] = "+".join(parts + [str(key).lower()])
    return found, unreadable


def local_paths(editor, home=None, system=None):
    """Where this editor keeps its keys on this machine, per platform.

    Returned rather than read so a caller can say which of them it found, and so
    the test can point them somewhere else.
    """
    home = home or os.path.expanduser("~")
    system = system or "/"
    if editor == "kdenlive":
        return [
            (os.path.join(home, ".config", "kdenliveshortcutsrc"), "kde"),
            (os.path.join(home, ".kde4", "share", "config", "kdenliveshortcutsrc"), "kde"),
        ]
    if editor == "finalcut":
        return [
            (os.path.join(home, "Library", "Application Support", "Final Cut Pro",
                          "Command Sets", "Default.commandset"), "commandset"),
            (os.path.join(system, "Applications", "Final Cut Pro.app", "Contents",
                          "Resources", "en.lproj", "Default.commandset"), "commandset"),
        ]
    if editor == "shotcut":
        return [
            (os.path.join(home, ".config", "Meltytech", "Shotcut.conf"), "ini"),
            (os.path.join(home, "Library", "Preferences",
                          "com.meltytech.Shotcut.plist"), "plist"),
        ]
    return []


def read_local(editor, action_names, home=None, system=None):
    """What this machine's own copy of the editor is set to.

    Nothing here is a default: a shortcut found in the person's own settings is
    the one their editor obeys, which is why these are the entries that get
    marked confirmed.
    """
    learned = {}
    notes = []
    for path, kind in local_paths(editor, home=home, system=system):
        if not os.path.exists(path):
            continue
        try:
            if kind == "kde":
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    found = from_kde_shortcuts(handle.read(), action_names)
            elif kind == "commandset":
                with open(path, "rb") as handle:
                    found, unreadable = from_final_cut_commandset(handle.read(), action_names)
                if unreadable:
                    notes.append(
                        "{}: {} command(s) are in this file in a shape this does "
                        "not read: {}".format(path, len(unreadable), ", ".join(unreadable))
                    )
            elif kind == "ini":
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    found = from_kde_shortcuts(handle.read(), action_names)
            else:
                continue
        except Exception as exc:               # a settings file this cannot read
            notes.append("{}: {}".format(path, exc))
            continue
        for name, chord in found.items():
            learned.setdefault(name, (chord, path))
    return learned, notes


def learn(editor, wanted, fetch=None, home=None, system=None, sources=None):
    """Everything that can be found out about this editor's keys.

    ``wanted`` maps this program's own step names to the editor's action names.
    ``fetch`` is what reads a URL, so the caller decides whether this is allowed
    to use the network at all - on a machine with none, the installed copy is
    still read.
    """
    action_names = [name for name in wanted.values() if name]
    learned = {}

    local, notes = read_local(editor, action_names, home=home, system=system)
    for name, (chord, path) in local.items():
        learned[name] = {"key": chord, "confirmed": True,
                         "source": "this machine: {}".format(path)}

    for url, reader in (sources or []):
        if fetch is None:
            break
        missing = [n for n in action_names if n not in learned]
        if not missing:
            break
        try:
            text = fetch(url)
        except Exception as exc:
            notes.append("could not read {}: {}".format(url, exc))
            continue
        try:
            if reader == "qt-source":
                found = from_qt_source(text, missing)
            elif reader == "qt-ui":
                found = from_qt_ui(text, missing)
            else:
                continue
        except Exception as exc:
            notes.append("could not make sense of {}: {}".format(url, exc))
            continue
        for name, chord in found.items():
            learned[name] = {
                "key": chord, "confirmed": False,
                "source": "the program's own source: {}".format(url),
            }

    out = {}
    for step_name, action_name in wanted.items():
        if action_name in learned:
            out[step_name] = dict(learned[action_name], action=action_name)
    return out, notes


def merge_into_keymap(keymap, learned):
    """Put what was learned into the keymap, keeping what it did not answer.

    A key nobody could learn keeps whatever was there and stays unconfirmed -
    it is still a guess, and the run says so before it presses anything.
    """
    changed = []
    keys = keymap.setdefault("keys", {})
    for name, found in learned.items():
        before = dict(keys.get(name) or {})
        entry = dict(before)
        entry.update({"key": found["key"], "confirmed": found["confirmed"],
                      "source": found["source"]})
        if before.get("key") != entry["key"] or before.get("confirmed") != entry["confirmed"]:
            changed.append((name, before.get("key"), entry["key"], found["source"]))
        keys[name] = entry
    return changed


def save_keymaps(path, maps):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        json.dump(maps, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
