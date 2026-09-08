"""The Common Edit Model (플러그인 기획안 37장, 38장; MVP 5).

37장 forbids the arrangement this codebase would otherwise drift into - the AI
Engine reaching for Premiere's API directly. It puts a layer between them:

    AI Engine  ->  Common Edit Model  ->  Editor Adapter  ->  편집기

So the engine's judgement is expressed once, in a structure that names no
editor, and each adapter turns that one structure into its own application's
calls. An adapter that has to re-read the edit plan is not an adapter; it is a
second implementation of the plan's meaning, and two of those disagree.

38장 names the structure: Project / Sequence / Track / Clip / SourceMedia /
InPoint / OutPoint / TimelinePosition / Audio / Subtitle / Text / Effect /
Transition / Marker. Those are the names used here.

What this deliberately does not do is decide the edit. 39장 keeps 장면 선택,
장면 순서, 편집 템포, 효과 사용 여부 and the rest with the AI; 40장 leaves the
program Timeline 생성 / Clip 생성 / Clip 이동 / Clip Trim / Audio 배치 /
Subtitle 배치 / Effect 적용. This module is entirely on the 40장 side: it
rearranges what the plan already says into the shape an editor can be driven
with, and adds nothing.

24장's track list (V3 텍스트·그래픽 / V2 보조 화면 / V1 주 영상 / A3 효과음 /
A2 BGM / A1 원본 음성) is an example, and the clause under it says 필요한
Track을 AI가 구성한다. So tracks here are created for what a plan actually
contains and for nothing else: a plan with no sound effect has no effects
track, rather than an empty one the operator has to look at and wonder about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "SourceMedia", "Effect", "Transition", "Clip", "AudioClip", "Subtitle",
    "Text", "Marker", "Track", "SequenceModel", "ProjectModel",
    "TIMELINE_MODES", "from_edit_plan",
]

#: 25장's two modes. A is the default because it keeps what the operator
#: already built: "기본값은 원본 보호를 위해 새로운 AI Sequence 생성을 권장한다".
#: B is only ever chosen explicitly by the person.
TIMELINE_MODES = ("new_sequence", "edit_current")


@dataclass
class SourceMedia:
    """A file the timeline draws from (38장 SourceMedia)."""

    media_id: str
    path: str
    duration_sec: float = 0.0
    has_video: bool = True
    has_audio: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "path": self.path,
            "duration_sec": round(self.duration_sec, 3),
            "has_video": self.has_video,
            "has_audio": self.has_audio,
        }


@dataclass
class Effect:
    """One effect on one clip (38장 Effect).

    ``kind`` is the plan's own word for it - zoom, crop, graphic - and
    ``params`` is what the plan said. Nothing is interpreted here: 39장 leaves
    효과 사용 여부 to the AI, and an adapter that cannot perform a kind reports
    that rather than this layer dropping it in advance.
    """

    kind: str
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": self.params}


@dataclass
class Transition:
    """A transition on one edge of a clip (38장 Transition)."""

    kind: str
    edge: str            # "in" or "out"
    duration_sec: float

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "edge": self.edge,
                "duration_sec": round(self.duration_sec, 3)}


@dataclass
class Clip:
    """One piece of source on the timeline (38장 Clip).

    InPoint / OutPoint are in the source; TimelinePosition is where it lands in
    the finished sequence. 2.4 of the main spec makes those two deliberately
    unrelated - a later moment may be shown first - so an adapter must never
    derive one from the other.
    """

    clip_id: str
    source_media_id: str
    in_point_sec: float
    out_point_sec: float
    timeline_position_sec: float
    name: str = ""
    speaker: str = ""
    role: str = ""
    #: Spans of the source *inside* this clip that pacing removed (9.3). Kept
    #: rather than pre-applied: an editor can place one clip and cut it, which
    #: is what a person would do, and the reason for each removal survives.
    remove_spans: list[list[float]] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    #: Level change the plan asked for on this clip's own audio, in dB.
    gain_db: float | None = None

    @property
    def duration_sec(self) -> float:
        """How much SOURCE this clip spans, removals included."""
        return max(0.0, self.out_point_sec - self.in_point_sec)

    @property
    def timeline_duration_sec(self) -> float:
        """How much of the TIMELINE it occupies, once the removals are gone.

        The two differ whenever pacing cut something out of the middle of a
        cut, and an adapter that places `duration_sec` of source at
        `timeline_position_sec` overlaps the next clip by exactly the length it
        was supposed to remove. Both are on the model because an editor needs
        one to trim the source and the other to lay the sequence out.
        """
        removed = sum(max(0.0, b - a) for a, b in self.remove_spans)
        return max(0.0, self.duration_sec - removed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "source_media_id": self.source_media_id,
            "in_point_sec": round(self.in_point_sec, 3),
            "out_point_sec": round(self.out_point_sec, 3),
            "timeline_position_sec": round(self.timeline_position_sec, 3),
            "duration_sec": round(self.duration_sec, 3),
            "timeline_duration_sec": round(self.timeline_duration_sec, 3),
            "name": self.name,
            "speaker": self.speaker,
            "role": self.role,
            "remove_spans": [[round(a, 3), round(b, 3)] for a, b in self.remove_spans],
            "effects": [e.as_dict() for e in self.effects],
            "transitions": [t.as_dict() for t in self.transitions],
            "gain_db": self.gain_db,
        }


@dataclass
class AudioClip:
    """Sound placed on the timeline that is not a video clip's own (38장 Audio).

    BGM and sound effects. ``source_media_id`` is empty when the plan named a
    file this model was not given - the adapter then reports a missing asset
    instead of silently leaving the sequence short of what the plan described.
    """

    clip_id: str
    source_media_id: str
    path: str
    timeline_position_sec: float
    duration_sec: float | None = None
    gain_db: float | None = None
    loop: bool = False
    name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "source_media_id": self.source_media_id,
            "path": self.path,
            "timeline_position_sec": round(self.timeline_position_sec, 3),
            "duration_sec": (round(self.duration_sec, 3)
                             if self.duration_sec is not None else None),
            "gain_db": self.gain_db,
            "loop": self.loop,
            "name": self.name,
        }


@dataclass
class Subtitle:
    """One caption on the finished timeline (38장 Subtitle)."""

    start_sec: float
    end_sec: float
    text: str
    speaker: str = ""
    style: str = ""
    emphasis: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_sec": round(self.start_sec, 3),
            "end_sec": round(self.end_sec, 3),
            "text": self.text,
            "speaker": self.speaker,
            "style": self.style,
            "emphasis": self.emphasis,
        }


@dataclass
class Text:
    """A title or on-screen line that is not a caption (38장 Text)."""

    start_sec: float
    end_sec: float
    text: str
    style: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"start_sec": round(self.start_sec, 3),
                "end_sec": round(self.end_sec, 3),
                "text": self.text, "style": self.style}


@dataclass
class Marker:
    """A note on the sequence for the person who opens it (38장 Marker)."""

    at_sec: float
    name: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"at_sec": round(self.at_sec, 3), "name": self.name, "note": self.note}


@dataclass
class Track:
    """One track of the sequence (38장 Track).

    ``kind`` is video / audio / subtitle / text. ``name`` is what 24장's example
    calls it - 주 영상, BGM, 효과음 - and ``index`` is the order within its
    kind, counting from 1, so an adapter can map it onto V1/V2 or A1/A2 without
    this layer knowing either naming.
    """

    kind: str
    name: str
    index: int = 1
    clips: list[Clip] = field(default_factory=list)
    audio: list[AudioClip] = field(default_factory=list)
    subtitles: list[Subtitle] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"kind": self.kind, "name": self.name, "index": self.index}
        if self.clips:
            row["clips"] = [c.as_dict() for c in self.clips]
        if self.audio:
            row["audio"] = [a.as_dict() for a in self.audio]
        if self.subtitles:
            row["subtitles"] = [s.as_dict() for s in self.subtitles]
        if self.texts:
            row["texts"] = [t.as_dict() for t in self.texts]
        return row


@dataclass
class SequenceModel:
    """One finished piece: a timeline the adapter builds (38장 Sequence)."""

    sequence_id: str
    name: str
    fps: float | None = None
    width: int = 0
    height: int = 0
    duration_sec: float = 0.0
    tracks: list[Track] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    def track(self, kind: str, name: str) -> Track:
        """The named track, created at the next free index if it is not there.

        24장: 필요한 Track을 AI가 구성한다. A track exists here because
        something in the plan needs it, never because a template listed it.
        """
        for existing in self.tracks:
            if existing.kind == kind and existing.name == name:
                return existing
        index = sum(1 for t in self.tracks if t.kind == kind) + 1
        made = Track(kind=kind, name=name, index=index)
        self.tracks.append(made)
        return made

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence_id": self.sequence_id,
            "name": self.name,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "duration_sec": round(self.duration_sec, 3),
            "tracks": [t.as_dict() for t in self.tracks],
            "markers": [m.as_dict() for m in self.markers],
            "notes": self.notes,
        }


@dataclass
class ProjectModel:
    """What an adapter is handed (38장 Project).

    ``mode`` is 25장's choice and it belongs to the person, not to this layer:
    `new_sequence` leaves whatever they had alone, `edit_current` changes it.
    """

    name: str
    media: list[SourceMedia] = field(default_factory=list)
    sequences: list[SequenceModel] = field(default_factory=list)
    mode: str = "new_sequence"
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.mode not in TIMELINE_MODES:
            raise ValueError(
                f"timeline mode {self.mode!r} is not one of {', '.join(TIMELINE_MODES)};"
                " 25장 has only those two, and edit_current is chosen explicitly"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "mode": self.mode,
            "media": [m.as_dict() for m in self.media],
            "sequences": [s.as_dict() for s in self.sequences],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8",
        )
        return target


# ---------------------------------------------------------------------------
# AI Engine -> Common Edit Model
# ---------------------------------------------------------------------------
#: 24장's example names, kept because they are the clause's own words for what
#: each track is for. Which of them a sequence gets is decided by the plan, not
#: by this list: a plan with no BGM has no BGM track.
TRACK_MAIN_VIDEO = "주 영상"
TRACK_GRAPHIC = "텍스트 / 그래픽"
TRACK_SOURCE_AUDIO = "원본 음성"
TRACK_BGM = "BGM"
TRACK_SFX = "효과음"
TRACK_SUBTITLE = "자막"


def from_edit_plan(
    plan: Any,
    *,
    name: str = "",
    mode: str = "new_sequence",
    source_duration_sec: float = 0.0,
    fps: float | None = None,
) -> ProjectModel:
    """Turn one edit plan into the model an adapter drives an editor with.

    Every value here comes from the plan. Nothing is chosen: the cut order, the
    in and out points, which effects apply, whether there is music at all -
    39장 leaves all of that with the AI, and re-deciding any of it here would
    make the timeline a different edit from the one the plan describes and that
    a person reviewed (8.2).

    ``source_duration_sec`` is only for the media entry, so an adapter that
    wants to check a clip against the file's length can. Zero means it was not
    supplied, not that the file is empty.
    """
    from aicut.render.timeline import Timeline

    settings = dict(getattr(plan, "render_settings", {}) or {})
    sequence = SequenceModel(
        sequence_id=plan.episode_id,
        name=name or f"AI_{plan.episode_id[:8]}",
        fps=fps if fps is not None else settings.get("fps"),
        width=int(settings.get("width") or 0),
        height=int(settings.get("height") or 0),
        notes={
            "target_type": getattr(plan, "target_type", ""),
            "structure": getattr(plan, "structure", {}) or {},
            # 17.5: a timeline built from unmeasured thresholds must say so
            # where the person doing the finishing will see it.
            "provenance": getattr(plan, "provenance", {}) or {},
        },
    )

    media = SourceMedia(
        media_id="source",
        path=plan.source_path,
        duration_sec=source_duration_sec,
    )

    timeline = Timeline.from_cuts(plan.cuts)
    cuts_by_order = {c.sequence_order: c for c in plan.cuts}
    video = sequence.track("video", TRACK_MAIN_VIDEO)

    # Pacing splits one cut into several segments when it removes a span from
    # inside it. Those pieces stay one clip here with the removals recorded:
    # 9.3 is a judgement about this cut, and an editor's own ripple delete is
    # how a person would carry it out - the reason survives the round trip.
    seen: set[int] = set()
    for segment in timeline.segments:
        if segment.sequence_order in seen:
            continue
        seen.add(segment.sequence_order)
        cut = cuts_by_order.get(segment.sequence_order)
        if cut is None:
            continue
        pieces = [s for s in timeline.segments if s.sequence_order == segment.sequence_order]
        visual = dict(getattr(cut, "visual_effect", {}) or {})
        audio_effect = dict(getattr(cut, "audio_effect", {}) or {})
        clip = Clip(
            clip_id=f"{plan.episode_id}-{cut.sequence_order:04d}",
            source_media_id=media.media_id,
            in_point_sec=cut.source_start_sec,
            out_point_sec=cut.source_end_sec,
            timeline_position_sec=segment.out_start_sec,
            name=f"{cut.scene_role or 'cut'} {cut.sequence_order}",
            speaker=getattr(cut, "speaker_tag", "") or "",
            role=getattr(cut, "scene_role", "") or "",
            remove_spans=[[float(a), float(b)] for a, b in
                          (tuple(s) for s in getattr(cut, "remove_spans", []) or [])],
            gain_db=(float(audio_effect["gain_db"])
                     if audio_effect.get("gain_db") is not None else None),
        )
        clip.effects = _effects_of(visual)
        clip.transitions = _transitions_of(visual, settings)
        video.clips.append(clip)
        del pieces

        graphic = visual.get("graphic")
        if graphic:
            spec = {"path": graphic} if isinstance(graphic, str) else dict(graphic)
            start = clip.timeline_position_sec + float(spec.get("start", 0.0))
            end = clip.timeline_position_sec + float(spec.get("end", clip.duration_sec))
            sequence.track("video", TRACK_GRAPHIC).texts.append(
                Text(start_sec=start, end_sec=end,
                     text=str(spec.get("path", "")), style="graphic")
            )

        sfx = audio_effect.get("sfx")
        if sfx:
            spec = {"path": sfx} if isinstance(sfx, str) else dict(sfx)
            sequence.track("audio", TRACK_SFX).audio.append(AudioClip(
                clip_id=f"{clip.clip_id}-sfx",
                source_media_id="",
                path=str(spec.get("path", "")),
                timeline_position_sec=clip.timeline_position_sec + float(spec.get("at", 0.0)),
                gain_db=(float(spec["gain_db"]) if spec.get("gain_db") is not None else None),
                name="sfx",
            ))

    sequence.duration_sec = timeline.duration

    # 24장 lists 원본 음성 as its own track. Both Premiere and Resolve place a
    # linked A/V clip, so this track carries no second copy of the media - it
    # records that the video clips' own audio belongs on its own track, which
    # is what an adapter needs to know to unlink them.
    if video.clips:
        sequence.track("audio", TRACK_SOURCE_AUDIO)

    bgm = (getattr(plan, "structure", {}) or {}).get("bgm")
    if bgm:
        spec = {"path": bgm} if isinstance(bgm, str) else dict(bgm)
        sequence.track("audio", TRACK_BGM).audio.append(AudioClip(
            clip_id=f"{plan.episode_id}-bgm",
            source_media_id="",
            path=str(spec.get("path", "")),
            timeline_position_sec=float(spec.get("start", 0.0)),
            duration_sec=(float(spec["duration"]) if spec.get("duration") is not None else None),
            gain_db=(float(spec["gain_db"]) if spec.get("gain_db") is not None else None),
            loop=bool(spec.get("loop", False)),
            name="bgm",
        ))

    lines = getattr(plan, "subtitles", []) or []
    if lines:
        track = sequence.track("subtitle", TRACK_SUBTITLE)
        for line in lines:
            track.subtitles.append(Subtitle(
                start_sec=line.start_sec,
                end_sec=line.end_sec,
                text=line.text,
                speaker=getattr(line, "speaker", "") or "",
                style=getattr(line, "style", "") or "",
                emphasis=bool(getattr(line, "emphasis", False)),
            ))

    # A marker where each cut begins: 29장 has the person check the result, and
    # the cut boundaries are where they would step through it.
    for cut_start, order in zip(timeline.cut_boundaries(), sorted(cuts_by_order)):
        cut = cuts_by_order[order]
        sequence.markers.append(Marker(
            at_sec=cut_start,
            name=cut.scene_role or f"cut {order}",
            note=getattr(cut, "pacing_reason", "") or "",
        ))

    return ProjectModel(
        name=name or plan.episode_id,
        media=[media],
        sequences=[sequence],
        mode=mode,
    )


def _effects_of(visual: dict[str, Any]) -> list[Effect]:
    """The plan's visual intent, as effects, in the plan's own words."""
    out: list[Effect] = []
    if visual.get("type") == "zoom" or visual.get("zoom") is not None:
        params = {k: v for k, v in visual.items()
                  if k in ("scale", "center", "keyframes", "zoom") and v is not None}
        out.append(Effect(kind="zoom", params=params))
    if visual.get("crop"):
        out.append(Effect(kind="crop", params={"crop": visual["crop"]}))
    return out


def _transitions_of(visual: dict[str, Any], settings: dict[str, Any]) -> list[Transition]:
    """10.2 전환, as the plan stated it.

    A bare string means both edges; a mapping says which. The length is the
    plan's, or the profile's default that the plan was rendered with - never a
    number chosen here.
    """
    stated = visual.get("transition")
    if not stated:
        return []
    default = float(settings.get("transition_sec") or 0.4)
    if isinstance(stated, str):
        spec = {"in": stated, "out": stated}
    else:
        spec = dict(stated)
    seconds = float(spec.get("duration", default))
    out: list[Transition] = []
    for edge in ("in", "out"):
        kind = spec.get(edge)
        if kind:
            out.append(Transition(kind=str(kind), edge=edge, duration_sec=seconds))
    return out
