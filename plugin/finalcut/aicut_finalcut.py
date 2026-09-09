"""Final Cut Pro Adapter (플러그인 기획안 37장).

    AI Engine -> Common Edit Model -> Final Cut Adapter -> Final Cut Pro

Final Cut Pro has no scripting API. There is no `insertClip` to call and no
interpreter inside the application to call it from, so the last arrow is a
document: this writes the sequence as FCPXML and hands the file to Final Cut,
which opens its own import dialog. That is the difference between this adapter
and the Resolve and Premiere ones, and it is the only difference - it reads the
same Common Edit Model, in the same order, and refuses the same things.

Two ways in, and the second is 4장's button:

    aicut_finalcut <model.json>      write the FCPXML for a model on disk
    aicut_finalcut --engine <file>   hand the broadcast to the engine, wait,
                                     and write what comes back

Run it with any Python 3.6+. Unlike the other two adapters this one is not
loaded by an editor, so it has no host interpreter to fit inside - but it stays
standard-library-only anyway, because a person who copied `plugin/` onto their
Mac has aicut's engine on another machine, or in another virtualenv, or not at
all.

NOT VERIFIED IN FINAL CUT. Final Cut Pro is macOS-only and is not present in
the environment this was written in, so the import itself has not been done.
What has: the FCPXML this writes is compared, cut for cut, against the FCPXML
`aicut export --format fcpxml` produces for the same episode - the exporter
whose output was checked against a real editor. Treat the first import as the
test.
"""

import argparse
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(os.path.dirname(_HERE), "common")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aicut_engine import Engine, EngineError, failure_reason  # noqa: E402
from aicut_model import (                               # noqa: E402
    ModelError,
    audio_clips,
    dropped_spans,
    kept_spans,
    load,
    media_path,
    sequences,
    subtitles,
    timeline_name,
    validated,
    video_clips,
)

#: FCPXML's own version. 1.9 is what Final Cut 10.4.9 and everything after it
#: read, and it is the version `aicut export` writes - the two have to agree or
#: the comparison between them proves nothing.
FCPXML_VERSION = "1.9"

#: Frame rates FCPXML states as a rational frame duration. The NTSC ones are
#: 1001/30000 rather than 1/29.97: a decimal rate is not frame-exact and Final
#: Cut rounds it in whichever direction it prefers, which shows up as a cut
#: landing a frame early somewhere in the middle of a long timeline.
_NTSC = {23.976: 24, 29.97: 30, 47.952: 48, 59.94: 60, 119.88: 120}


def frame_duration(fps):
    """FCPXML's `frameDuration`, as (numerator, denominator)."""
    if fps <= 0:
        raise ModelError("frame rate must be positive, got {}".format(fps))
    for rate, base in _NTSC.items():
        if abs(fps - rate) < 0.01:
            return 1001, base * 1000
    if float(fps).is_integer():
        return 1, int(fps)
    raise ModelError(
        "{} is not a frame rate FCPXML can state exactly. Build the sequence at "
        "one of {}, or at a whole number of frames a second.".format(
            fps, ", ".join(str(r) for r in sorted(_NTSC))
        )
    )


def rational(seconds, frame_num, frame_den):
    """A time as FCPXML writes them: a whole number of frames, as `N/Ds`.

    Everything is quantised to the frame here rather than left for the importer
    to round: two clips rounded opposite ways leave a one-frame hole between
    them, which is a black flash in the finished video.
    """
    frames = int(round(seconds * (float(frame_den) / frame_num)))
    return "{}/{}s".format(frames * frame_num, frame_den)


def media_src(path):
    """A `file://` URI for the source, which is what makes the timeline relink."""
    absolute = os.path.abspath(path)
    try:
        from urllib.parse import quote
    except ImportError:                       # pragma: no cover - py2 hosts
        from urllib import quote
    if absolute[1:3] in (":\\", ":/"):        # C:\... on Windows
        return "file:///" + quote(absolute.replace("\\", "/"), safe="/:")
    return "file://" + quote(absolute, safe="/")


def sequence_fps(sequence, stated=None):
    """The frame rate to build at, refusing to guess one.

    The engine writes the plan's own rate into the model. A model that does not
    carry one is not a reason to pick 30: every cut in the timeline is placed
    against this number, and a wrong one slides all of them.
    """
    if stated:
        return float(stated)
    fps = sequence.get("fps")
    if fps:
        return float(fps)
    raise ModelError(
        "this model does not say what frame rate it was planned at, so the "
        "sequence cannot be built without guessing one. Pass --fps with the "
        "rate of the footage."
    )


def to_fcpxml(model, sequence, fps, event_name=None):
    """The sequence as an FCPXML document Final Cut can import.

    Deliberately the same shape as `aicut export --format fcpxml`: one asset for
    the broadcast and one `asset-clip` per surviving span, in timeline order.
    The test compares the two documents clip for clip, so a change to either
    that the other does not make is a failure rather than a difference nobody
    notices until an import looks wrong.
    """
    frame_num, frame_den = frame_duration(fps)

    def at(seconds):
        return rational(seconds, frame_num, frame_den)

    source = media_path(model, "source")
    name = os.path.basename(source)
    stem = name.rpartition(".")[0] or name
    media = [m for m in model.get("media", []) if m.get("media_id") == "source"][0]

    width = int(sequence.get("width") or 0) or 1920
    height = int(sequence.get("height") or 0) or 1080

    clips = []
    offset = 0.0
    for clip in video_clips(sequence):
        for start_sec, end_sec in kept_spans(clip):
            frames = int(round(end_sec * (float(frame_den) / frame_num))) \
                - int(round(start_sec * (float(frame_den) / frame_num)))
            if frames <= 0:
                continue                      # reported by dropped_spans
            clips.append((offset, start_sec, frames * frame_num / float(frame_den),
                          clip.get("name") or stem))
            offset += frames * frame_num / float(frame_den)
    if not clips:
        raise ModelError(
            "every clip in this sequence is shorter than one frame at {} fps".format(fps)
        )

    duration = media.get("duration_sec") or max(end for _, end, _, _ in clips)

    fcpxml = ElementTree.Element("fcpxml", {"version": FCPXML_VERSION})
    resources = ElementTree.SubElement(fcpxml, "resources")
    ElementTree.SubElement(resources, "format", {
        "id": "r1", "name": "AicutSequence",
        "frameDuration": "{}/{}s".format(frame_num, frame_den),
        "width": str(width), "height": str(height),
    })
    # The asset carries the SOURCE's shape. The model states the sequence's, so
    # when they differ the person has to relink - said out loud by `build`
    # rather than left to look like the plan asked for a letterbox.
    ElementTree.SubElement(resources, "format", {
        "id": "r3", "name": "AicutSource",
        "frameDuration": "{}/{}s".format(frame_num, frame_den),
        "width": str(width), "height": str(height),
    })
    asset = ElementTree.SubElement(resources, "asset", {
        "id": "r2", "name": name, "start": "0s", "duration": at(duration),
        "hasVideo": "1" if media.get("has_video", True) else "0",
        "hasAudio": "1" if media.get("has_audio", True) else "0",
        "format": "r3",
    })
    ElementTree.SubElement(asset, "media-rep", {
        "kind": "original-media", "src": media_src(source),
    })

    library = ElementTree.SubElement(fcpxml, "library")
    event = ElementTree.SubElement(library, "event", {
        "name": event_name or model.get("name") or "aicut",
    })
    project = ElementTree.SubElement(event, "project", {
        "name": timeline_name(model, sequence),
    })
    seq_element = ElementTree.SubElement(project, "sequence", {
        "format": "r1", "duration": at(offset), "tcStart": "0s", "tcFormat": "NDF",
    })
    spine = ElementTree.SubElement(seq_element, "spine")
    for clip_offset, source_start, clip_duration, clip_name in clips:
        ElementTree.SubElement(spine, "asset-clip", {
            "ref": "r2", "name": clip_name,
            "offset": at(clip_offset), "start": at(source_start),
            "duration": at(clip_duration),
        })

    try:
        # Wrapped rather than required: `indent` arrived in 3.9 and this may be
        # run by whatever Python a Mac has. Final Cut does not care either way;
        # the person reading the file to check a time does.
        ElementTree.indent(fcpxml, space="  ")
    except AttributeError:                    # pragma: no cover - Python < 3.9
        pass
    body = ElementTree.tostring(fcpxml, encoding="utf-8").decode("utf-8")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n" + body + "\n"
    )


def write(model, sequence, path, fps=None, event_name=None):
    """Write one sequence's FCPXML, and say what the format cannot carry."""
    rate = sequence_fps(sequence, fps)
    text = to_fcpxml(model, sequence, rate, event_name=event_name)
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    # The document declares UTF-8 in its first line, so it has to be written in
    # it: Python's text mode follows the machine's locale, and on a Korean
    # Windows that is cp949 - the XML would say one encoding and be another.
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)

    print("{} -> {}".format(timeline_name(model, sequence), target))
    for start, end in dropped_spans(sequence, rate):
        print("  skipped {:.3f}-{:.3f}s: shorter than one frame at {} fps".format(
            start, end, rate))
    placed = audio_clips(sequence)
    if placed:
        # 24장 puts BGM and 효과음 on their own tracks. FCPXML can carry a second
        # audio lane, but only for media declared as an asset - and the model
        # states a path, not a duration or a frame rate for it. Saying so beats
        # writing an asset with numbers nobody measured.
        print("  {} audio placement(s) the model asks for are not in the XML:".format(
            len(placed)))
        for track_name, clip in placed:
            print("    {} {} at {:.2f}s".format(track_name, clip.get("path", ""),
                                                clip.get("timeline_position_sec", 0.0)))
    markers = sequence.get("markers") or []
    if markers:
        # 29장 has the person check the result and the markers are where they
        # would step through it. FCPXML can carry them, but on the clip they sit
        # inside rather than on the timeline, and a marker placed on the wrong
        # clip is worse than a marker the person adds themselves - so this says
        # how many there are instead of guessing where they go.
        print("  {} marker(s) in the model are not in the XML".format(len(markers)))

    lines = subtitles(sequence)
    if lines:
        print("  {} caption(s) in the model; import an .srt for them "
              "(`aicut export <plan> --format srt`)".format(len(lines)))
    return target


def open_in_final_cut(path):
    """Hand the file to Final Cut, which opens its own import dialog.

    Not an import: there is no API for that. On anything but a Mac this says so
    rather than pretending the file went somewhere.
    """
    if sys.platform != "darwin":
        print("  not macOS: import {} in Final Cut with File > Import > XML".format(path))
        return False
    try:
        subprocess.check_call(["open", "-a", "Final Cut Pro", path])
    except (OSError, subprocess.CalledProcessError) as exc:
        print("  could not hand the file to Final Cut ({}); import it with "
              "File > Import > XML".format(exc))
        return False
    return True


def build_from_file(model_path, out_dir=None, fps=None, open_after=True):
    model = load(model_path)
    written = []
    for sequence in sequences(model):
        target = os.path.join(
            out_dir or os.path.dirname(os.path.abspath(model_path)),
            "{}.fcpxml".format(timeline_name(model, sequence)),
        )
        written.append(write(model, sequence, target, fps=fps))
        if open_after:
            open_in_final_cut(written[-1])
    return written


def build_from_engine(source_path, out_dir=None, fps=None, mode="new_sequence",
                      engine=None, poll_sec=3.0, open_after=True):
    """4장's button: hand the engine the broadcast, wait, write what comes back.

    The plugin analyses nothing (35장). Final Cut cannot tell it what is on a
    timeline either, so the broadcast is named on the command line rather than
    read out of the application.
    """
    engine = engine or Engine()
    print("analysing {}".format(os.path.basename(source_path)))
    job = engine.submit(source_path)
    job_id = job.get("job_id") or job.get("id")
    project_id = job.get("project_id") or ""
    if not job_id:
        raise EngineError("the engine did not return a job id: {!r}".format(job))

    # 26장's progress panel is this loop's output. The engine names its own
    # stages; repeating them is the panel, and inventing stage names here would
    # describe a pipeline that is not the one running.
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
                "{}.fcpxml".format(timeline_name(model, sequence)),
            )
            written.append(write(model, sequence, target, fps=fps))
            if open_after:
                open_in_final_cut(written[-1])
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aicut_finalcut",
        description="Build a Final Cut Pro timeline from an aicut Common Edit Model",
    )
    parser.add_argument("model", nargs="?", help="a Common Edit Model .json")
    parser.add_argument("--engine", metavar="BROADCAST",
                        help="hand this file to the engine and build what comes back")
    parser.add_argument("--out", help="where to write the .fcpxml (default: beside the model)")
    parser.add_argument("--fps", type=float,
                        help="frame rate to build at, when the model does not say")
    parser.add_argument("--mode", default="new_sequence",
                        choices=["new_sequence", "edit_current"],
                        help="25장's two modes; Final Cut imports into the library either way")
    parser.add_argument("--no-open", action="store_true",
                        help="write the file and stop, rather than handing it to Final Cut")
    args = parser.parse_args(argv)

    if not args.model and not args.engine:
        parser.error("give a model file, or --engine <broadcast> to have one made")
    try:
        if args.engine:
            build_from_engine(args.engine, out_dir=args.out, fps=args.fps,
                              mode=args.mode, open_after=not args.no_open)
        else:
            build_from_file(args.model, out_dir=args.out, fps=args.fps,
                            open_after=not args.no_open)
    except (ModelError, EngineError) as exc:
        print("aicut: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
