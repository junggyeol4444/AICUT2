"""Avid Media Composer Adapter (플러그인 기획안 37장).

    AI Engine -> Common Edit Model -> Avid Adapter -> Media Composer

Media Composer has no live scripting API either, so like the Final Cut adapter
the last arrow is a document. The document is a CMX 3600 EDL, which Media
Composer reads through its own EDL import, and this writes it from the Common
Edit Model rather than from the edit plan - one meaning of the model, however
many editors read it.

    aicut_avid <model.json>         write the EDL for a model on disk
    aicut_avid --engine <file>      hand the broadcast to the engine, wait,
                                    and write what comes back

What an EDL cannot carry is most of the plan: it is cuts and nothing else - no
source file path (an EDL names a reel and the editor relinks), no captions, no
effects, no music. Everything the model asks for that does not fit is printed
rather than dropped in silence. AAF would carry more, but writing one needs a
library outside the standard library and this runs wherever the operator's Avid
is, so the EDL is what it writes and what it says it writes.

NOT VERIFIED IN MEDIA COMPOSER. Avid is not present in the environment this was
written in, so the import itself has not been done. What has: the EDL this
writes is compared, event for event, against the EDL `aicut export --format
edl` produces for the same episode. Treat the first import as the test.
"""

import argparse
import math
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(os.path.dirname(_HERE), "common")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aicut_engine import Engine, EngineError, failure_reason   # noqa: E402
from aicut_model import (                                      # noqa: E402
    ModelError,
    audio_clips,
    dropped_spans,
    kept_spans,
    media_path,
    load,
    sequences,
    subtitles,
    timeline_name,
    validated,
    video_clips,
)

#: Frame rates a non-drop-frame timecode can be written for without lying about
#: it. 23.976, 29.97 and 59.94 are counted at the nearest whole rate, which is
#: what non-drop-frame timecode is; drop-frame is not written at all rather than
#: written wrongly. The same table the engine's exporter uses - the tests
#: compare the two documents, so a rate one accepts and the other does not is a
#: failure here rather than a surprise at import.
TC_BASE = {23.976: 24, 24: 24, 25: 25, 29.97: 30, 30: 30, 50: 50, 59.94: 60, 60: 60}


def tc_base(fps):
    for known, base in TC_BASE.items():
        if math.isclose(fps, known, rel_tol=1e-3):
            return base
    if float(fps).is_integer() and 1 <= fps <= 240:
        return int(fps)
    raise ModelError(
        "{} fps has no non-drop-frame timecode this writes. Pass --fps with the "
        "rate the Avid sequence uses (24, 25, 30, 50, 60, or "
        "23.976/29.97/59.94).".format(fps)
    )


def timecode(seconds, fps):
    """HH:MM:SS:FF, non-drop-frame.

    The frame is counted at the media's real rate and labelled at the whole one.
    At 29.97 the timeline's hour is frame 107892 and reads `00:59:56:12`;
    writing `01:00:00:00` sends Media Composer to frame 108000, 3.6 seconds
    late - and this adapter is checked against the exporter, which had the same
    fault.
    """
    if seconds < 0:
        raise ModelError("negative time {}".format(seconds))
    base = tc_base(fps)
    total = int(round(seconds * float(fps)))
    frames = total % base
    total //= base
    return "{:02d}:{:02d}:{:02d}:{:02d}".format(
        total // 3600, (total % 3600) // 60, total % 60, frames,
    )


def source_name(path):
    """The file's own name, whichever platform wrote the path."""
    return path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path


def reel_name(path):
    """EDL reel names are 8 characters of A-Z0-9 in practice.

    Anything else is mangled differently by every editor, so it is normalised
    here - the same way the engine's exporter normalises it, because an EDL
    whose reel differs from the one the operator already relinked is a second
    relink for no reason.
    """
    name = source_name(path)
    stem = name.rpartition(".")[0] or name
    cleaned = "".join(c for c in stem.upper() if ord(c) < 128 and c.isalnum())
    return (cleaned or "AICUT")[:8].ljust(8)


def sequence_fps(sequence, stated=None):
    """The rate to write timecode at, refusing to guess one.

    Every event in the EDL is placed against this number. A wrong one does not
    fail; it lands every cut a little late, which is only found by watching.
    """
    if stated:
        return float(stated)
    fps = sequence.get("fps")
    if fps:
        return float(fps)
    raise ModelError(
        "this model does not say what frame rate it was planned at, so timecode "
        "cannot be written without guessing one. Pass --fps with the rate the "
        "Avid sequence uses."
    )


def to_edl(model, sequence, fps, title=None):
    """The sequence as a CMX 3600 EDL.

    Deliberately the same shape as `aicut export --format edl`: one event per
    surviving span, in timeline order, cut only. The test compares the two
    documents line for line.
    """
    source = media_path(model, "source")
    reel = reel_name(source)
    name = source_name(source)
    lines = [
        "TITLE: {}".format(title or model.get("name") or timeline_name(model, sequence)),
        "FCM: NON-DROP FRAME",
    ]
    index = 0
    record = 0.0
    for clip in video_clips(sequence):
        for start_sec, end_sec in kept_spans(clip):
            frames = int(round(end_sec * float(fps))) - int(round(start_sec * float(fps)))
            if frames <= 0:
                continue                      # reported by dropped_spans
            index += 1
            # Seconds of media, at the media's rate - the record timecode is
            # written from this, and dividing by the label rate instead stretched
            # every NTSC event by a thousandth.
            duration = frames / float(fps)
            lines.append("{:03d}  {} AA/V  C        {} {} {} {}".format(
                index, reel,
                timecode(start_sec, fps), timecode(end_sec, fps),
                timecode(record, fps), timecode(record + duration, fps),
            ))
            lines.append("* FROM CLIP NAME: {}".format(name))
            record += duration
    if not index:
        raise ModelError(
            "every clip in this sequence is shorter than one frame at {} fps".format(fps)
        )
    return "\n".join(lines) + "\n"


def write(model, sequence, path, fps=None, title=None):
    """Write one sequence's EDL, and say what the format cannot carry."""
    rate = sequence_fps(sequence, fps)
    text = to_edl(model, sequence, rate, title=title)
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)

    print("{} -> {}".format(timeline_name(model, sequence), target))
    print("  reel {} - Media Composer asks which clip that is on import; an EDL "
          "names no file".format(reel_name(media_path(model, "source")).strip()))
    for start, end in dropped_spans(sequence, rate):
        print("  skipped {:.3f}-{:.3f}s: shorter than one frame at {} fps".format(
            start, end, rate))

    placed = audio_clips(sequence)
    if placed:
        print("  {} audio placement(s) the model asks for are not in the EDL:".format(
            len(placed)))
        for track_name, clip in placed:
            print("    {} {} at {:.2f}s".format(track_name, clip.get("path", ""),
                                                clip.get("timeline_position_sec", 0.0)))
    markers = sequence.get("markers") or []
    if markers:
        print("  {} marker(s) in the model are not in the EDL".format(len(markers)))
    lines = subtitles(sequence)
    if lines:
        print("  {} caption(s) in the model; import an .srt for them "
              "(`aicut export <plan> --format srt`)".format(len(lines)))
    effects = 0
    for clip in video_clips(sequence):
        effects += len(clip.get("effects") or []) + len(clip.get("transitions") or [])
    if effects:
        # 10.2's zooms, crops and transitions. An EDL is cuts; saying so is the
        # difference between a person adding them back and never knowing.
        print("  {} effect(s)/transition(s) in the model are not in the EDL".format(effects))
    return target


def build_from_file(model_path, out_dir=None, fps=None):
    model = load(model_path)
    written = []
    for sequence in sequences(model):
        target = os.path.join(
            out_dir or os.path.dirname(os.path.abspath(model_path)),
            "{}.edl".format(timeline_name(model, sequence)),
        )
        written.append(write(model, sequence, target, fps=fps))
    return written


def build_from_engine(source_path, out_dir=None, fps=None, mode="new_sequence",
                      engine=None, poll_sec=3.0):
    """4장's button: hand the engine the broadcast, wait, write what comes back."""
    engine = engine or Engine()
    print("analysing {}".format(os.path.basename(source_path)))
    job = engine.submit(source_path)
    job_id = job.get("job_id") or job.get("id")
    project_id = job.get("project_id") or ""
    if not job_id:
        raise EngineError("the engine did not return a job id: {!r}".format(job))

    # 26장's progress panel is this loop's output. The engine names its own
    # stages; inventing names here would describe a pipeline that is not running.
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

    written = []
    for episode in episodes:
        model = validated(engine.edit_model(episode["episode_id"], mode=mode))
        for sequence in sequences(model):
            target = os.path.join(
                out_dir or os.getcwd(),
                "{}.edl".format(timeline_name(model, sequence)),
            )
            written.append(write(model, sequence, target, fps=fps))
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aicut_avid",
        description="Write an Avid Media Composer EDL from an aicut Common Edit Model",
    )
    parser.add_argument("model", nargs="?", help="a Common Edit Model .json")
    parser.add_argument("--engine", metavar="BROADCAST",
                        help="hand this file to the engine and write what comes back")
    parser.add_argument("--out", help="where to write the .edl (default: beside the model)")
    parser.add_argument("--fps", type=float,
                        help="frame rate to write timecode at, when the model does not say")
    parser.add_argument("--title", help="the EDL's TITLE line (default: the model's name)")
    parser.add_argument("--mode", default="new_sequence",
                        choices=["new_sequence", "edit_current"],
                        help="25장's two modes; an EDL is imported as its own sequence either way")
    args = parser.parse_args(argv)

    if not args.model and not args.engine:
        parser.error("give a model file, or --engine <broadcast> to have one made")
    try:
        if args.engine:
            build_from_engine(args.engine, out_dir=args.out, fps=args.fps, mode=args.mode)
        else:
            build_from_file(args.model, out_dir=args.out, fps=args.fps)
    except (ModelError, EngineError) as exc:
        print("aicut: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
