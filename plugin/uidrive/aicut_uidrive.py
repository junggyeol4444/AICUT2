"""AI 자동 편집, in an editor that has no plugin of its own (2장, 4장, 5장).

    AI Engine -> Common Edit Model -> Steps -> the editor's own window

Resolve, Premiere and VEGAS let a plugin call them, and those three have one.
Final Cut, Avid, Shotcut and Kdenlive do not - so this works them the way a
person does: it brings the editor forward and types.

    aicut_uidrive --editor shotcut --dry-run <model.json>
    aicut_uidrive --editor shotcut <model.json>
    aicut_uidrive --editor shotcut --engine <broadcast>

Run the dry run first. It prints every key before anything is pressed, and the
keys come from `keymaps.json`, where none of them has been confirmed against a
running copy of any of these editors - so the first thing to do with this is
read that list and fix what is wrong.

NOT RUN AGAINST ANY OF THE FOUR EDITORS. None of them is installed on the
machine this was written on. What has been run: the Linux driver, against a
real X client that recorded what arrived - `00:01:40:00` came out as those
eleven characters with shift held for each colon, and `ctrl+s` arrived as s
with the control modifier set.
"""

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(os.path.dirname(_HERE), "common")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aicut_driver import DriverError, for_this_machine          # noqa: E402
from aicut_engine import Engine, EngineError, failure_reason    # noqa: E402
from aicut_model import (                                       # noqa: E402
    ModelError,
    audio_clips,
    dropped_spans,
    load,
    sequences,
    subtitles,
    validated,
)
from aicut_steps import (                                       # noqa: E402
    build_steps,
    describe,
    load_keymap,
    unconfirmed,
)


def sequence_fps(sequence, stated=None):
    """The rate the timeline is being built at, refusing to guess one."""
    if stated:
        return float(stated)
    fps = sequence.get("fps")
    if fps:
        return float(fps)
    raise ModelError(
        "this model does not say what frame rate it was planned at. Pass --fps "
        "with the rate of the sequence that is open in the editor - every cut "
        "is typed against it."
    )


def edit(model, sequence, editor, fps=None, mode="new_sequence",
         dry_run=False, focus=True, driver=None, keymap_path=None):
    """Carry one sequence out in the editor that is already open."""
    keymap = load_keymap(editor, keymap_path)
    rate = sequence_fps(sequence, fps)
    steps = build_steps(model, sequence, rate, keymap, mode=mode)

    not_checked = unconfirmed(keymap)
    if not_checked:
        # 17.1's habit: a value nobody measured is said out loud rather than
        # left to look like a fact. These are somebody else's shortcuts.
        print("  {} of {}'s keys have never been confirmed against a running "
              "copy: {}".format(len(not_checked), keymap.get("name", editor),
                                ", ".join(not_checked)))
        print("  read the list below, fix keymaps.json, and only then run it "
              "for real")

    driver = driver or for_this_machine(dry_run=dry_run)
    if dry_run:
        print(describe(steps))
        print("  {} steps. Nothing was pressed.".format(len(steps)))
        return steps

    if focus:
        driver.focus(keymap.get("window", [keymap.get("name", editor)]))

    def say(index, total, step):
        # 26장's progress panel: what it is doing, in the words of why.
        print("  [{}/{}] {}".format(index, total, step.why))

    driver.run(steps, on_step=say)

    for start, end in dropped_spans(sequence, rate):
        print("  skipped {:.3f}-{:.3f}s: shorter than one frame at {} fps".format(
            start, end, rate))
    placed = audio_clips(sequence)
    if placed:
        # 24장's BGM and 효과음. Typing a music track in needs the file's own
        # length and level, which the model states for the broadcast and not
        # for these - so they are named rather than half-done.
        print("  {} audio placement(s) the model asks for were not made:".format(
            len(placed)))
        for track_name, clip in placed:
            print("    {} {} at {:.2f}s".format(track_name, clip.get("path", ""),
                                                clip.get("timeline_position_sec", 0.0)))
    lines = subtitles(sequence)
    if lines:
        print("  {} caption(s) in the model were not typed in; import an .srt "
              "for them (`aicut export <plan> --format srt`)".format(len(lines)))
    return steps


def edit_from_file(model_path, editor, **kwargs):
    model = load(model_path)
    done = []
    for sequence in sequences(model):
        done.append(edit(model, sequence, editor, **kwargs))
    return done


def edit_from_engine(source_path, editor, engine=None, poll_sec=3.0, mode="new_sequence",
                     **kwargs):
    """4장's button, for an editor that cannot hold one.

    The person has the broadcast open in the editor; this hands the engine the
    same file, waits, and then works the editor with what comes back.
    """
    engine = engine or Engine()
    print("analysing {}".format(os.path.basename(source_path)))
    job = engine.submit(source_path)
    job_id = job.get("job_id") or job.get("id")
    project_id = job.get("project_id") or ""
    if not job_id:
        raise EngineError("the engine did not return a job id: {!r}".format(job))

    last = ""
    while True:
        state = engine.job(job_id)
        line = state.get("state") or state.get("status") or ""
        if line and line != last:
            print("  " + line)
            last = line
        if not state.get("running", False):
            break
        time.sleep(poll_sec)
    failed = failure_reason(state)
    if failed:
        raise EngineError("the analysis failed: {}".format(failed))
    project_id = state.get("project_id") or project_id
    if not project_id:
        raise EngineError("the engine did not say which project it made")

    episodes = engine.episodes(project_id)
    if not episodes:
        # 16장: 제작 가치 있는 콘텐츠 없음 is a normal ending, not a failure.
        print("the engine found nothing worth producing in this broadcast (16장)")
        return []

    done = []
    for episode in episodes:
        model = validated(engine.edit_model(episode["episode_id"], mode=mode))
        for sequence in sequences(model):
            done.append(edit(model, sequence, editor, mode=mode, **kwargs))
    return done


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aicut_uidrive",
        description="Work an editor that has no plugin, the way a person does",
    )
    parser.add_argument("model", nargs="?", help="a Common Edit Model .json")
    parser.add_argument("--editor", required=True,
                        help="which editor is open (see keymaps.json)")
    parser.add_argument("--engine", metavar="BROADCAST",
                        help="hand this file to the engine, then edit what comes back")
    parser.add_argument("--fps", type=float,
                        help="the open sequence's frame rate, when the model does not say")
    parser.add_argument("--mode", default="new_sequence",
                        choices=["new_sequence", "edit_current"],
                        help="25장's two modes")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every key and press nothing")
    parser.add_argument("--no-focus", action="store_true",
                        help="do not bring the editor forward; click it yourself first")
    args = parser.parse_args(argv)

    if not args.model and not args.engine:
        parser.error("give a model file, or --engine <broadcast> to have one made")
    try:
        if args.engine:
            edit_from_engine(args.engine, args.editor, fps=args.fps, mode=args.mode,
                             dry_run=args.dry_run, focus=not args.no_focus)
        else:
            edit_from_file(args.model, args.editor, fps=args.fps, mode=args.mode,
                           dry_run=args.dry_run, focus=not args.no_focus)
    except (ModelError, EngineError, DriverError) as exc:
        print("aicut: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
