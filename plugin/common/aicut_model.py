"""Reading the Common Edit Model inside an editor (플러그인 기획안 37장, 38장).

37장 puts this structure between the AI Engine and any editor API:

    AI Engine -> Common Edit Model -> Editor Adapter -> 편집기

So an adapter reads this and never the edit plan. A plugin that re-read the
plan would be a second implementation of the plan's meaning, and two of those
disagree - which is what this file exists to prevent. Every decision an adapter
needs is answered here, and nothing here touches an editor, so it can be tested
on a machine that has neither Resolve nor Premiere on it, which is the machine
it was written on.

Standard library only, and Python 3.6 syntax: this runs inside the editor's own
interpreter, not in the aicut virtualenv.
"""

import json
import os

#: Resolve's `endFrame` is the LAST frame of the clip, not one past it. Off by
#: one here means every cut in the timeline is a frame long or a frame short,
#: which nobody notices until the export.
END_FRAME_IS_INCLUSIVE = True


class ModelError(Exception):
    """The model cannot be turned into a timeline."""


def load(path):
    """Read a Common Edit Model file, with errors a person can act on."""
    if not os.path.exists(path):
        raise ModelError("no edit model at {}".format(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            model = json.load(handle)
    except ValueError as exc:
        raise ModelError("{} is not valid JSON: {}".format(path, exc))
    return validated(model)


def validated(model):
    """Refuse a document that is not a Common Edit Model, saying which it is."""
    if not isinstance(model, dict):
        raise ModelError("an edit model is a JSON object")
    if "cuts" in model and "sequences" not in model:
        raise ModelError(
            "this is an aicut edit plan, not a Common Edit Model. 37장 has the "
            "adapter read the model: ask the engine for "
            "/api/episodes/<id>/edit-model, or write one with "
            "`aicut export --format edit-model`."
        )
    if "sequences" not in model:
        raise ModelError("this file has no 'sequences'; it is not a Common Edit Model")
    if not model["sequences"]:
        raise ModelError("this model has no sequence in it - there is no timeline to build")
    return model


def media_path(model, media_id):
    """Where the file for one media id is, as the model states it."""
    for media in model.get("media", []) or []:
        if media.get("media_id") == media_id:
            path = media.get("path") or ""
            if not path:
                raise ModelError("media {} has no path".format(media_id))
            return path
    raise ModelError("the model has no media called {}".format(media_id))


def sequences(model):
    return list(model.get("sequences", []) or [])


def tracks(sequence, kind):
    """The sequence's tracks of one kind, in index order.

    24장: 필요한 Track을 AI가 구성한다. There are as many as the model has and
    no more, so an adapter creating a fixed set would add empty ones.
    """
    found = [t for t in sequence.get("tracks", []) or [] if t.get("kind") == kind]
    return sorted(found, key=lambda t: t.get("index", 1))


def kept_spans(clip):
    """The parts of one clip that survive pacing, in source seconds.

    A clip carries the spans an editor must drop from inside it (9.3); a
    timeline that ignores them plays the dead air the plan decided to remove.
    """
    spans = [(float(clip["in_point_sec"]), float(clip["out_point_sec"]))]
    for removal in sorted(tuple(r) for r in clip.get("remove_spans", []) or []):
        start, end = float(removal[0]), float(removal[1])
        out = []
        for a, b in spans:
            if end <= a or start >= b:
                out.append((a, b))
                continue
            if start > a:
                out.append((a, min(start, b)))
            if end < b:
                out.append((max(end, a), b))
        spans = out
    return [(a, b) for a, b in spans if b - a > 1e-3]


def video_clips(sequence):
    """Every clip of the main video track, in timeline order.

    Timeline order, not source order: 2.4 lets a video open on a moment that
    happened last, and sorting by in-point would quietly rebuild the broadcast.
    """
    video = tracks(sequence, "video")
    if not video:
        raise ModelError("this sequence has no video track")
    clips = list(video[0].get("clips", []) or [])
    return sorted(clips, key=lambda c: c.get("timeline_position_sec", 0.0))


def frame_ranges(sequence, fps):
    """Every surviving span as (start_frame, end_frame), in timeline order.

    The frame rate is the editor's, not the model's: a sequence built at a
    different rate slides every cut, and the sequence the adapter is filling is
    the one that decides.
    """
    if fps <= 0:
        raise ModelError("frame rate must be positive, got {}".format(fps))
    ranges = []
    for clip in video_clips(sequence):
        for start_sec, end_sec in kept_spans(clip):
            start_frame = int(round(start_sec * fps))
            end_frame = int(round(end_sec * fps))
            if END_FRAME_IS_INCLUSIVE:
                end_frame -= 1
            if end_frame < start_frame:
                # Shorter than one frame at this rate. Reported by
                # `dropped_spans` rather than silently shifting what follows.
                continue
            ranges.append((start_frame, end_frame, clip))
    if not ranges:
        raise ModelError(
            "every clip in this sequence is shorter than one frame at {} fps".format(fps)
        )
    return ranges


def dropped_spans(sequence, fps):
    """Spans too short to survive at this frame rate, so the caller can say so."""
    dropped = []
    for clip in video_clips(sequence):
        for start_sec, end_sec in kept_spans(clip):
            if int(round(end_sec * fps)) - 1 < int(round(start_sec * fps)):
                dropped.append((start_sec, end_sec))
    return dropped


def timeline_name(model, sequence):
    """A name a person can find again, not a bare id.

    25장's default is a new sequence beside what the operator built, so the
    name is what tells the two apart in their bin.
    """
    stated = (sequence.get("name") or "").strip()
    if stated:
        return stated
    return "AI_{}".format(str(sequence.get("sequence_id", "sequence"))[:8])


def subtitles(sequence):
    """Every caption, in time order, from whatever subtitle tracks exist."""
    lines = []
    for track in tracks(sequence, "subtitle"):
        lines.extend(track.get("subtitles", []) or [])
    return sorted(lines, key=lambda s: s.get("start_sec", 0.0))


def audio_clips(sequence):
    """Sound the model places itself - BGM and effects - with its track name.

    The video clips' own audio is not here: 24장 gives 원본 음성 its own track
    and both Resolve and Premiere place a linked A/V clip, so an adapter that
    also laid these down would double the source audio.
    """
    placed = []
    for track in tracks(sequence, "audio"):
        for clip in track.get("audio", []) or []:
            placed.append((track.get("name", ""), clip))
    return placed


def summary(model, sequence, fps):
    """One paragraph a person can check the timeline against."""
    ranges = frame_ranges(sequence, fps)
    total = sum(end - start + 1 for start, end, _ in ranges)
    source = os.path.basename(media_path(model, "source")) if model.get("media") else "?"
    return "{} clips, {:.1f}s at {} fps, from {}".format(
        len(ranges), total / float(fps), fps, source,
    )
