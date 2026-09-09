"""Shotcut / Kdenlive Adapter (플러그인 기획안 37장).

    AI Engine -> Common Edit Model -> MLT Adapter -> Shotcut, Kdenlive

Neither editor has a live scripting API, and both are built on the same engine:
MLT. Their project file is MLT XML, so one adapter serves both - Shotcut opens
the file it writes directly, and Kdenlive imports it as a project.

    aicut_mlt <model.json>          write the .mlt for a model on disk
    aicut_mlt --engine <file>       hand the broadcast to the engine, wait,
                                    and write what comes back

MLT counts in frames and its `out` is the LAST frame of a clip, not one past it
- the same convention Resolve uses and the opposite of Premiere's. It is stated
here as a constant for the same reason it is stated there: one frame either way
on every cut is invisible until the export.

NOT VERIFIED IN SHOTCUT OR KDENLIVE. Neither is present in the environment this
was written in, so the file this writes has not been opened by them. What has
been checked: it is well-formed XML, every entry lands on the frames the model
reader says survive, the playlist is contiguous, and the whole document is
internally consistent - every producer an entry names exists, and the tractor's
tracks exist. Treat the first open as the test.
"""

import argparse
import os
import sys
import time
import xml.etree.ElementTree as ElementTree

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(os.path.dirname(_HERE), "common")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aicut_engine import Engine, EngineError, failure_reason   # noqa: E402
from aicut_model import (                                      # noqa: E402
    ModelError,
    audio_clips,
    dropped_spans,
    frame_ranges,
    load,
    media_path,
    sequences,
    subtitles,
    timeline_name,
    validated,
)

#: MLT's `out` is the last frame of the clip, not one past it. `frame_ranges`
#: already answers in that convention when asked for it, so this is the flag
#: rather than a subtraction buried in the writer.
OUT_FRAME_IS_INCLUSIVE = True

#: MLT states the rate as a fraction. The NTSC rates are 30000/1001 and not
#: 29.97: a decimal rate drifts a frame every thousand or so, which on a
#: six-hour broadcast is minutes.
_NTSC = {23.976: (24000, 1001), 29.97: (30000, 1001),
         47.952: (48000, 1001), 59.94: (60000, 1001), 119.88: (120000, 1001)}


def frame_rate(fps):
    """MLT's `frame_rate_num` / `frame_rate_den`."""
    if fps <= 0:
        raise ModelError("frame rate must be positive, got {}".format(fps))
    for rate, fraction in _NTSC.items():
        if abs(fps - rate) < 0.01:
            return fraction
    if float(fps).is_integer():
        return int(fps), 1
    raise ModelError(
        "{} fps is not a rate MLT can state exactly. Build at one of {}, or at a "
        "whole number of frames a second.".format(
            fps, ", ".join(str(r) for r in sorted(_NTSC))
        )
    )


def sequence_fps(sequence, stated=None):
    """The rate to build at, refusing to guess one."""
    if stated:
        return float(stated)
    fps = sequence.get("fps")
    if fps:
        return float(fps)
    raise ModelError(
        "this model does not say what frame rate it was planned at, so the "
        "timeline cannot be built without guessing one. Pass --fps with the rate "
        "of the footage."
    )


def to_mlt(model, sequence, fps, name=None):
    """The sequence as an MLT XML document.

    One producer for the broadcast and one playlist entry per surviving span, in
    timeline order. The tractor carries a black background track under it, which
    is what both editors expect to find and what keeps the timeline's length
    honest when the first entry does not start at zero.
    """
    num, den = frame_rate(fps)
    ranges = frame_ranges(sequence, fps)          # already out-inclusive
    source = media_path(model, "source")
    media = [m for m in model.get("media", []) if m.get("media_id") == "source"][0]
    duration = float(media.get("duration_sec") or 0.0)
    total = sum(end - start + 1 for start, end, _clip in ranges)

    width = int(sequence.get("width") or 0) or 1920
    height = int(sequence.get("height") or 0) or 1080

    mlt = ElementTree.Element("mlt", {
        "LC_NUMERIC": "C",
        "producer": "main_bin",
        "root": "",
        "title": name or timeline_name(model, sequence),
    })
    ElementTree.SubElement(mlt, "profile", {
        "description": "aicut",
        "width": str(width), "height": str(height),
        "progressive": "1",
        "sample_aspect_num": "1", "sample_aspect_den": "1",
        "display_aspect_num": str(width), "display_aspect_den": str(height),
        "frame_rate_num": str(num), "frame_rate_den": str(den),
        "colorspace": "709",
    })

    # The black track under everything. Both editors write one; without it a
    # timeline whose first entry is not at zero has nothing to sit on.
    background = ElementTree.SubElement(mlt, "producer", {
        "id": "background", "in": "0",
        "out": str(max(0, total - 1)),
    })
    _property(background, "length", str(total))
    _property(background, "mlt_service", "color")
    _property(background, "resource", "black")
    _property(background, "aspect_ratio", "1")

    source_frames = int(round(duration * num / float(den))) if duration else None
    producer = ElementTree.SubElement(mlt, "producer", {
        "id": "producer0", "in": "0",
        "out": str(source_frames - 1) if source_frames else "0",
    })
    if source_frames:
        _property(producer, "length", str(source_frames))
    _property(producer, "resource", source)
    _property(producer, "mlt_service", "avformat")
    _property(producer, "audio_index", "-1" if not media.get("has_audio", True) else "0")

    playlist = ElementTree.SubElement(mlt, "playlist", {"id": "playlist0"})
    for start_frame, end_frame, clip in ranges:
        entry = ElementTree.SubElement(playlist, "entry", {
            "producer": "producer0",
            "in": str(start_frame),
            "out": str(end_frame),
        })
        # 38장 names the pieces; the model's own name for a cut is what a person
        # reads on the clip, so it goes on rather than a generated one.
        if clip.get("name"):
            _property(entry, "aicut:name", clip["name"])

    background_track = ElementTree.SubElement(mlt, "playlist", {"id": "background_playlist"})
    ElementTree.SubElement(background_track, "entry", {
        "producer": "background", "in": "0", "out": str(max(0, total - 1)),
    })

    tractor = ElementTree.SubElement(mlt, "tractor", {
        "id": "tractor0", "title": name or timeline_name(model, sequence),
        "in": "0", "out": str(max(0, total - 1)),
    })
    ElementTree.SubElement(tractor, "track", {"producer": "background_playlist"})
    ElementTree.SubElement(tractor, "track", {"producer": "playlist0"})

    try:
        ElementTree.indent(mlt, space="  ")
    except AttributeError:                    # pragma: no cover - Python < 3.9
        pass
    body = ElementTree.tostring(mlt, encoding="utf-8").decode("utf-8")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n"


def _property(parent, name, value):
    node = ElementTree.SubElement(parent, "property", {"name": name})
    node.text = value
    return node


def write(model, sequence, path, fps=None, name=None):
    """Write one sequence's .mlt, and say what it does not carry."""
    rate = sequence_fps(sequence, fps)
    text = to_mlt(model, sequence, rate, name=name)
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    # utf-8 explicitly: the document says so in its first line, and Python's
    # text mode otherwise follows the machine's locale.
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)

    print("{} -> {}".format(timeline_name(model, sequence), target))
    for start, end in dropped_spans(sequence, rate):
        print("  skipped {:.3f}-{:.3f}s: shorter than one frame at {} fps".format(
            start, end, rate))
    placed = audio_clips(sequence)
    if placed:
        # 24장 puts BGM and 효과음 on their own tracks. MLT can carry another
        # track, but a producer needs the file's length and rate, which the
        # model states for the broadcast and not for these.
        print("  {} audio placement(s) the model asks for are not in the XML:".format(
            len(placed)))
        for track_name, clip in placed:
            print("    {} {} at {:.2f}s".format(track_name, clip.get("path", ""),
                                                clip.get("timeline_position_sec", 0.0)))
    markers = sequence.get("markers") or []
    if markers:
        print("  {} marker(s) in the model are not in the XML".format(len(markers)))
    lines = subtitles(sequence)
    if lines:
        print("  {} caption(s) in the model; import an .srt for them "
              "(`aicut export <plan> --format srt`)".format(len(lines)))
    return target


def build_from_file(model_path, out_dir=None, fps=None):
    model = load(model_path)
    written = []
    for sequence in sequences(model):
        target = os.path.join(
            out_dir or os.path.dirname(os.path.abspath(model_path)),
            "{}.mlt".format(timeline_name(model, sequence)),
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
                "{}.mlt".format(timeline_name(model, sequence)),
            )
            written.append(write(model, sequence, target, fps=fps))
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aicut_mlt",
        description="Build a Shotcut/Kdenlive timeline from an aicut Common Edit Model",
    )
    parser.add_argument("model", nargs="?", help="a Common Edit Model .json")
    parser.add_argument("--engine", metavar="BROADCAST",
                        help="hand this file to the engine and build what comes back")
    parser.add_argument("--out", help="where to write the .mlt (default: beside the model)")
    parser.add_argument("--fps", type=float,
                        help="frame rate to build at, when the model does not say")
    parser.add_argument("--name", help="the timeline's name (default: the model's)")
    parser.add_argument("--mode", default="new_sequence",
                        choices=["new_sequence", "edit_current"],
                        help="25장's two modes; an .mlt is opened as its own project either way")
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
