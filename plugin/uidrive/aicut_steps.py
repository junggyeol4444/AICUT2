"""What a person does to the editor, in order (플러그인 기획안 2장, 5장).

    AI Engine -> Common Edit Model -> Editor Steps -> the editor itself

Three editors let a plugin call them (Resolve, Premiere, VEGAS). The rest do
not, and the answer is not to hand the person a file and ask them to open it -
2장 is four steps for the person and no more:

    1. 영상 편집기 실행
    2. 생방송 영상 가져오기
    3. 플러그인 실행
    4. "AI 자동 편집" 버튼 클릭

So for those editors the program works the editor the way a person does: it
types and clicks in the application that is already open, with the broadcast
already on the timeline. This module decides WHAT to do and in what order; it
touches no keyboard and no window, so it can be tested on a machine with none
of these editors installed - which is the machine it was written on.

Every step is one of a small set: press keys, type text, click a menu, wait for
the application to catch up. What those keys are per editor is in
`keymaps.json`, because they are the editor's, not this program's - and the
ones that have not been confirmed against a running copy are marked there
rather than presented as fact.
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(os.path.dirname(_HERE), "common")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aicut_model import (                                      # noqa: E402
    ModelError,
    frame_ranges,
    media_path,
    timeline_name,
)

KEYMAPS = os.path.join(_HERE, "keymaps.json")

#: The step kinds. A driver knows how to carry out these four and nothing else,
#: which is what keeps "what to do" separable from "how to press a key".
KEYS = "keys"          #: a chord or a sequence of chords, e.g. "cmd+i"
TYPE = "type"          #: literal text, e.g. a timecode typed into a field
MENU = "menu"          #: a named menu path, e.g. ["File", "Import", "Media..."]
WAIT = "wait"          #: seconds to let the application catch up


class Step(object):
    """One thing to do, with the reason it is being done.

    The reason is not decoration: a run that is watched (26장) shows these, and
    a step nobody can explain is a step nobody can check.
    """

    def __init__(self, kind, value, why, seconds=0.0):
        self.kind = kind
        self.value = value
        self.why = why
        self.seconds = seconds

    def __repr__(self):                       # pragma: no cover - debugging aid
        return "Step({!r}, {!r})".format(self.kind, self.value)


def load_keymap(editor, path=None):
    """The editor's own keys, as stated in `keymaps.json`."""
    with open(path or KEYMAPS, "r", encoding="utf-8") as handle:
        maps = json.load(handle)
    if editor not in maps:
        raise ModelError(
            "no keymap for {}. The ones this knows are {}.".format(
                editor, ", ".join(sorted(k for k in maps if not k.startswith("_")))
            )
        )
    return maps[editor]


def unconfirmed(keymap):
    """Keys not read from the copy of the editor installed on this machine.

    17.1's habit, applied to somebody else's application. There are three states
    a key can be in and they are not the same thing:

    * confirmed - read from this machine's own settings, which is the shortcut
      the editor obeys;
    * sourced - read from the program's own source code, so it is what the
      editor ships with, but not what this person may have changed it to;
    * neither - written down by hand from documentation, which is a guess.

    This answers the first question; :func:`unsourced` answers the third state,
    and the caller prints both before touching the keyboard.
    """
    return sorted(name for name, key in keymap.get("keys", {}).items()
                  if not key.get("confirmed"))


def unsourced(keymap):
    """Keys that were never learned from anywhere - hand-written guesses."""
    return sorted(name for name, key in keymap.get("keys", {}).items()
                  if not key.get("source"))


def timecode(seconds, fps):
    """HH:MM:SS:FF, which is what these editors' timecode fields take."""
    if seconds < 0:
        raise ModelError("negative time {}".format(seconds))
    base = int(round(fps))
    total = int(round(seconds * base))
    frames = total % base
    total //= base
    return "{:02d}:{:02d}:{:02d}:{:02d}".format(
        total // 3600, (total % 3600) // 60, total % 60, frames,
    )


def build_steps(model, sequence, fps, keymap, mode="new_sequence"):
    """The whole edit, as the steps a person would take.

    The shape is the one every editor's manual describes for assembling from a
    source: open the clip, mark in, mark out, append to the timeline, repeat.
    5장's first three steps - 현재 편집기 프로젝트 확인, 현재 Timeline에 존재하는
    영상 확인, 원본 미디어 확인 - happen before this, in the driver, because
    they are questions about the running application rather than about the model.
    """
    keys = keymap.get("keys", {})

    def key(name, why, seconds=0.0):
        stated = keys.get(name)
        if not stated:
            raise ModelError(
                "{} has no '{}' in its keymap, so this edit cannot be carried "
                "out by keyboard. Add it to keymaps.json.".format(
                    keymap.get("name", "this editor"), name)
            )
        return Step(KEYS, stated["key"], why, seconds)

    steps = []
    ranges = frame_ranges(sequence, fps)
    source = media_path(model, "source")

    steps.append(Step(WAIT, None, "let the editor finish whatever it is doing",
                      seconds=keymap.get("settle_sec", 0.5)))

    if mode == "new_sequence":
        # 25장 mode A: the operator's own timeline is left alone and the AI's
        # work goes on a new one. The default, and the reason it is the default
        # is that the other way edits something they made.
        steps.append(key("new_timeline", "25장 A: a new timeline, so the "
                                         "operator's own is untouched", 1.0))
        steps.append(Step(TYPE, timeline_name(model, sequence),
                          "name it so they can find it"))
        steps.append(key("confirm", "accept the new timeline", 1.0))

    steps.append(key("focus_source", "put the broadcast in the source viewer", 0.5))

    for index, (start_frame, end_frame, clip) in enumerate(ranges, start=1):
        start_sec = start_frame / float(fps)
        # frame_ranges answers with an inclusive last frame; a person types the
        # out point as that frame's timecode, which is the same number.
        end_sec = end_frame / float(fps)
        why = "cut {}/{}: {}".format(index, len(ranges), clip.get("name") or "")
        steps.append(key("goto_timecode", why + " - jump to its start"))
        steps.append(Step(TYPE, timecode(start_sec, fps), why + " - the in point"))
        steps.append(key("confirm", why + " - go there", 0.2))
        steps.append(key("mark_in", why + " - mark in"))
        steps.append(key("goto_timecode", why + " - jump to its end"))
        steps.append(Step(TYPE, timecode(end_sec, fps), why + " - the out point"))
        steps.append(key("confirm", why + " - go there", 0.2))
        steps.append(key("mark_out", why + " - mark out"))
        steps.append(key("append", why + " - append it to the timeline", 0.3))

    steps.append(key("save", "save, so a crash does not cost the whole run", 1.0))
    return steps


def describe(steps):
    """The dry run: what it would do, in the words of why it is doing it."""
    lines = []
    for index, step in enumerate(steps, start=1):
        if step.kind == WAIT:
            lines.append("{:3d}. wait {:.1f}s - {}".format(index, step.seconds, step.why))
        elif step.kind == TYPE:
            lines.append("{:3d}. type {!r} - {}".format(index, step.value, step.why))
        elif step.kind == MENU:
            lines.append("{:3d}. menu {} - {}".format(index, " > ".join(step.value), step.why))
        else:
            lines.append("{:3d}. press {} - {}".format(index, step.value, step.why))
    return "\n".join(lines)
