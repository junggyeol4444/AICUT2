"""The renderer (10장). It executes an edit plan and judges nothing (10.1).

Three corrections from 10.4 are implemented here rather than described:

**10.4-1 face-tracking zoom.** ``crop=...:x=face_center_x:y=face_center_y`` does
not work: those are not ffmpeg variables, and ``crop`` cannot be steered
per-frame from nothing. Two strategies are implemented instead - ``segment_crop``
(split the zoom into segments, one fixed crop each, then concat: simple, stepped
camera) and ``sendcmd`` (drive crop's x/y over time from a generated command
file: smooth, more complex graph). Which one wins is an MVP 6 measurement, so the
choice is a profile parameter, not a constant. The third option in 10.4 -
frame-by-frame compositing outside ffmpeg - is deliberately not implemented here;
it belongs in a separate processing pipeline if measurement ever justifies it.

**10.4-2 cut joins.** ``acrossfade`` per join would build a filter graph with
hundreds of nested crossfades. Each segment instead gets a few-millisecond
``afade`` in/out to kill the click, and joining is done by concat.

**10.4-3 loudness.** EBU R128 normalisation stays, but as a measure-then-apply
two-pass, so a timeline assembled from a dozen places in the broadcast does not
drift in level between them.
"""

from __future__ import annotations

import logging
import shutil
import dataclasses
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from aicut.config import CalibrationProfile
from aicut.errors import RenderError
from aicut.media.audio import LoudnessStats, measure_loudness, parse_loudnorm_json
from aicut.media.ffmpeg_util import LIBASS_HINT, require_ffmpeg, require_filter, run
from aicut.render.editplan import EditPlan
from aicut.render.timeline import Segment, Timeline

log = logging.getLogger(__name__)


@dataclass
class RenderSettings:
    video_codec: str = "libx264"
    preset: str = "medium"
    crf: int = 18
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    sample_rate: int = 48000
    height: int = 1080
    width: int | None = None
    fps: float | None = None
    cut_fade_ms: int = 8
    zoom_strategy: str = "segment_crop"
    #: Default length of a 10.2 전환 when the plan asks for one without saying
    #: how long. Overridable per transition; a profile value like the rest.
    transition_sec: float = 0.4
    loudness_i: float = -14.0
    loudness_tp: float = -1.0
    loudness_lra: float = 11.0
    two_pass_loudness: bool = True

    @classmethod
    def from_profile(cls, profile: CalibrationProfile, *, target_type: str = "") -> "RenderSettings":
        video = profile.get("render.video")
        audio = profile.get("render.audio")
        loud = profile.get("render.audio.loudness")
        settings = cls(
            video_codec=video["codec"],
            preset=video["preset"],
            crf=int(video["crf"]),
            pix_fmt=video["pix_fmt"],
            audio_codec=audio["codec"],
            audio_bitrate=audio["bitrate"],
            sample_rate=int(audio["sample_rate"]),
            height=int(video["default_height"]),
            fps=video.get("fps"),
            cut_fade_ms=int(audio["cut_fade_ms"]),
            transition_sec=float(video.get("transition_sec", 0.4)),
            zoom_strategy=profile.get("render.zoom.strategy"),
            loudness_i=float(loud["integrated_lufs"]),
            loudness_tp=float(loud["true_peak_dbtp"]),
            loudness_lra=float(loud["loudness_range"]),
            two_pass_loudness=bool(loud["two_pass"]),
        )
        if target_type and "short" in target_type.lower():
            width, height = profile.get("render.video.shorts_resolution")
            settings.width, settings.height = int(width), int(height)
        return settings

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def with_plan(self, stored: dict[str, Any] | None) -> "RenderSettings":
        """Apply the settings the plan was written with. They win.

        10.1 says the renderer executes the plan and decides nothing, and 8.2
        says a person must be able to read and amend that plan. Rebuilding
        codec, resolution, frame rate, zoom strategy and loudness from whatever
        profile happens to be current breaks both: re-rendering a saved plan
        after a re-calibration produced a different file from the one that was
        reviewed, and hand-edits to the plan's `render` block did nothing at all.

        Keys the plan does not state keep the profile's value, so a plan written
        by an older version still renders. Unknown keys are ignored rather than
        raising: a plan is an interchange file and may outlive this field list.
        """
        if not stored:
            return self
        known = {field.name for field in dataclasses.fields(self)}
        updates: dict[str, Any] = {}
        for key, value in stored.items():
            if key not in known or value is None:
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                # bool("false") is True, so a hand-edited string needs saying.
                updates[key] = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
                continue
            if current is None or isinstance(value, type(current)):
                updates[key] = value
                continue
            try:
                updates[key] = type(current)(value)
            except (TypeError, ValueError):
                log.warning("plan render setting %s=%r is not usable; keeping %r", key, value, current)
        return dataclasses.replace(self, **updates)


# ---------------------------------------------------------------------------
# filter construction
# ---------------------------------------------------------------------------
def zoom_filter(effect: dict[str, Any], settings: RenderSettings) -> str:
    """Build the crop chain for one zoomed segment (10.4-1, strategy a).

    ``center`` is normalised 0..1 in the source frame; the crop window is
    clamped so it can never leave the frame, which is the other half of why the
    original expression could not work.
    """
    scale = float(effect.get("scale", 0.83))
    scale = max(0.2, min(1.0, scale))
    cx, cy = effect.get("center", [0.5, 0.5])
    cx = max(0.0, min(1.0, float(cx)))
    cy = max(0.0, min(1.0, float(cy)))
    # x = centre - half a window, clamped into [0, in_w - window]
    x = f"min(max(iw*{cx:.4f}-iw*{scale:.4f}/2\\,0)\\,iw-iw*{scale:.4f})"
    y = f"min(max(ih*{cy:.4f}-ih*{scale:.4f}/2\\,0)\\,ih-ih*{scale:.4f})"
    return f"crop=w=iw*{scale:.4f}:h=ih*{scale:.4f}:x={x}:y={y}"


def sendcmd_file(keyframes: Sequence[dict[str, Any]], path: str | Path) -> Path:
    """Write a sendcmd script that walks crop's x/y over time (10.4-1, strategy b).

    Each keyframe is ``{"at_sec": t, "scale": s, "center": [cx, cy]}`` in
    segment-local time. ffmpeg applies each command at its timestamp, so the
    camera moves in steps as fine as the keyframes are dense.

    **This path pans; it does not zoom.** crop marks ``w`` and ``h``
    runtime-commandable, but sending them stalls the graph: measured on ffmpeg
    7.1, a segment whose crop size changes mid-stream hangs with the process
    idle (60s wall, 0.5s CPU, no output), because the downstream scale filter
    never reconfigures. Only ``x`` and ``y`` are sent, at one constant crop
    size, and a keyframe list carrying more than one scale is flattened with a
    warning rather than silently pretending to zoom.

    A zoom that actually changes magnification therefore belongs to the
    ``segment_crop`` strategy, which splits the change into segments and gets a
    stepped camera. Which of the two a channel prefers is the MVP 6
    measurement 10.4 asks for; this is one of its inputs.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(keyframes, key=lambda k: float(k.get("at_sec", 0.0)))
    scales = {round(float(kf.get("scale", 0.83)), 4) for kf in ordered}
    scale = max(0.2, min(1.0, float(ordered[0].get("scale", 0.83)))) if ordered else 0.83
    if len(scales) > 1:
        log.warning(
            "sendcmd zoom holds %d different scales %s; crop cannot resize mid-segment without"
            " stalling the graph, so the camera pans at scale %.2f. Use the segment_crop strategy"
            " for a magnification change (10.4-1).",
            len(scales), sorted(scales), scale,
        )
    lines = []
    for kf in ordered:
        at = float(kf.get("at_sec", 0.0))
        cx, cy = kf.get("center", [0.5, 0.5])
        cx = max(0.0, min(1.0, float(cx)))
        cy = max(0.0, min(1.0, float(cy)))
        x = f"min(max(iw*{cx:.4f}-iw*{scale:.4f}/2,0),iw-iw*{scale:.4f})"
        y = f"min(max(ih*{cy:.4f}-ih*{scale:.4f}/2,0),ih-ih*{scale:.4f})"
        lines.append(f"{at:.3f} crop x '{x}', crop y '{y}';")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def crop_filter(effect: dict[str, Any]) -> str:
    """A standalone crop, separate from the zoom of 10.4-1 (10.2 크롭).

    Two forms the plan can state, and nothing else:

    * ``"crop": "9:16"`` - keep that aspect out of the middle of the frame.
      This is what a Shorts cut of a 16:9 broadcast needs.
    * ``"crop": {"x": .., "y": .., "w": .., "h": ..}`` - normalised 0..1 box.

    10.1 gives the renderer no discretion, so an unparseable value is dropped
    with a warning rather than guessed at.
    """
    crop = effect.get("crop")
    if not crop:
        return ""
    if isinstance(crop, dict):
        w = max(0.01, min(1.0, float(crop.get("w", 1.0))))
        h = max(0.01, min(1.0, float(crop.get("h", 1.0))))
        x = max(0.0, min(1.0 - w, float(crop.get("x", (1.0 - w) / 2))))
        y = max(0.0, min(1.0 - h, float(crop.get("y", (1.0 - h) / 2))))
        return f"crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f}"
    text = str(crop)
    if ":" in text:
        try:
            num, den = (float(part) for part in text.split(":", 1))
        except ValueError:
            log.warning("crop %r is not an aspect or a box; ignoring", crop)
            return ""
        if num <= 0 or den <= 0:
            log.warning("crop %r has a non-positive side; ignoring", crop)
            return ""
        # Take the largest window of that aspect that fits, centred.
        return (
            f"crop=w='min(iw,ih*{num / den:.6f})':h='min(ih,iw*{den / num:.6f})'"
            ":x='(iw-ow)/2':y='(ih-oh)/2'"
        )
    log.warning("crop %r is not an aspect or a box; ignoring", crop)
    return ""


def transition_filters(effect: dict[str, Any], duration: float, settings: RenderSettings):
    """The join into and out of this cut (10.2 전환).

    10.4-2 rules out a per-join filter on the joined graph, and the reasoning
    holds for video as well as audio: hundreds of ``xfade`` pairs is a graph
    ffmpeg has to hold at once. So a transition is expressed on the segment
    itself, as a fade at its edges, and the cuts are still joined by concat.

    It is a fade through black, not a cross dissolve. The plan asks for
    ``"transition": "fade"`` or ``{"in": "fade", "out": "fade", "sec": 0.4}``
    and gets exactly that; a name this does not implement is dropped with a
    warning rather than silently rendered as something else (10.1).
    """
    transition = effect.get("transition")
    if not transition:
        return []
    if isinstance(transition, str):
        transition = {"in": transition, "out": transition}
    seconds = float(transition.get("sec", settings.transition_sec))
    seconds = max(0.0, min(seconds, duration / 2 if duration else seconds))
    if seconds <= 0:
        return []
    out: list[str] = []
    for edge, key in (("in", "in"), ("out", "out")):
        name = transition.get(key)
        if not name:
            continue
        if name not in ("fade", "cut"):
            log.warning("transition %r is not implemented; rendering a hard cut", name)
            continue
        if name == "cut":
            continue
        if edge == "in":
            out.append(f"fade=t=in:st=0:d={seconds:.3f}")
        else:
            out.append(f"fade=t=out:st={max(0.0, duration - seconds):.3f}:d={seconds:.3f}")
    return out


def scale_filter(settings: RenderSettings) -> str:
    """Fit to the output frame; pad rather than stretch when the aspect differs."""
    if settings.width:
        return (
            f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=increase,"
            f"crop={settings.width}:{settings.height}"
        )
    return f"scale=-2:{settings.height}"


def audio_edge_filter(duration: float, settings: RenderSettings) -> str:
    """Millisecond fades at both ends of a segment (10.4-2)."""
    fade = max(0.0, settings.cut_fade_ms / 1000.0)
    if fade <= 0 or duration <= fade * 2:
        return "anull"
    return f"afade=t=in:st=0:d={fade:.4f},afade=t=out:st={duration - fade:.4f}:d={fade:.4f}"


def build_segment_command(
    source: str,
    segment: Segment,
    out_path: str,
    settings: RenderSettings,
    *,
    visual_effect: dict[str, Any] | None = None,
    audio_effect: dict[str, Any] | None = None,
    sendcmd_path: str | None = None,
    audio_streams: int = 1,
) -> list[str]:
    """ffmpeg args that cut one segment out of the source and normalise its shape.

    Seeking is done with ``-ss`` before ``-i`` (fast) plus ``-accurate_seek`` so
    the frame the plan asked for is the frame that lands in the file.

    `audio_streams` is how many audio streams the source carries. 5.2 assumes a
    multi-track recording — mic, call, game, BGM on separate streams — and
    mapping only ``0:a:0`` would put whichever stream happens to be first into
    the finished video and discard the rest, permanently, before the concat.
    Depending on the recorder's stream order that silently drops the host's
    voice or all of the game and call audio. So every stream is mixed.

    `normalize=0` on the mix is deliberate: amix otherwise divides by the input
    count, which would quiet a four-track broadcast to a quarter. Absolute level
    is not this command's job — the 2-pass EBU R128 pass of 10.4-3 sets it on
    the joined timeline.
    """
    effect = visual_effect or {}
    filters: list[str] = []
    if effect.get("type") == "zoom":
        if settings.zoom_strategy == "sendcmd" and sendcmd_path:
            # Same escaping as the subtitle path: this is a filter argument, and
            # a Windows command file at C:\... would otherwise end the option
            # at the drive colon.
            filters.append(f"sendcmd=f='{_escape_filter_path(sendcmd_path)}'")
            filters.append(zoom_filter(effect, settings))
        else:
            filters.append(zoom_filter(effect, settings))
    # 10.2 크롭: separate from the zoom above, and applied before the scale so
    # the framing is chosen in source pixels.
    crop = crop_filter(effect)
    if crop:
        filters.append(crop)
    filters.append(scale_filter(settings))
    if settings.fps:
        filters.append(f"fps={settings.fps}")
    filters.append("setsar=1")
    # 10.2 전환, as a fade on this segment's own edges - see transition_filters
    # for why it is not a per-join filter.
    filters.extend(transition_filters(effect, segment.duration, settings))

    audio_filters = [audio_edge_filter(segment.duration, settings)]
    gain = float((audio_effect or {}).get("gain_db", 0.0))
    if gain:
        audio_filters.insert(0, f"volume={gain}dB")
    audio_filters.append(f"aresample={settings.sample_rate}")

    head = [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-accurate_seek", "-ss", f"{segment.source_start_sec:.3f}",
        "-t", f"{segment.duration:.3f}",
        "-i", source,
    ]
    tail = [
        "-c:v", settings.video_codec, "-preset", settings.preset, "-crf", str(settings.crf),
        "-pix_fmt", settings.pix_fmt,
        "-c:a", settings.audio_codec, "-b:a", settings.audio_bitrate, "-ar", str(settings.sample_rate),
    ]

    # 10.2 그래픽 and 효과음 each bring a file of their own, so they become extra
    # inputs and the whole thing has to go through the complex graph.
    graphic = _graphic_input(effect)
    sfx = _sfx_input(audio_effect or {})
    extra: list[str] = []
    next_input = 1
    graphic_index = sfx_index = None
    if graphic:
        extra += ["-i", graphic["path"]]
        graphic_index = next_input
        next_input += 1
    if sfx:
        extra += ["-i", sfx["path"]]
        sfx_index = next_input
        next_input += 1

    complex_needed = audio_streams > 1 or graphic_index is not None or sfx_index is not None
    if not complex_needed:
        return head + [
            "-vf", ",".join(filters),
            "-af", ",".join(audio_filters),
        ] + tail + ["-map", "0:v:0", "-map", "0:a:0?", out_path]

    # -vf and -filter_complex cannot both be given, so the video chain moves
    # into the complex graph unchanged when there is anything to combine.
    chains: list[str] = []
    video_label = "[v]"
    if graphic_index is None:
        chains.append(f"[0:v:0]{','.join(filters)}[v]")
    else:
        chains.append(f"[0:v:0]{','.join(filters)}[vbase]")
        chains.append(
            f"[{graphic_index}:v]scale={graphic['width']}:-1[gfx]"
            if graphic["width"] else f"[{graphic_index}:v]null[gfx]"
        )
        chains.append(
            f"[vbase][gfx]overlay=x={graphic['x']}:y={graphic['y']}"
            f":enable='between(t,{graphic['start']:.3f},{graphic['end']:.3f})'[v]"
        )

    if audio_streams > 1:
        sources = "".join(f"[0:a:{index}]" for index in range(audio_streams))
        chains.append(
            f"{sources}amix=inputs={audio_streams}:normalize=0,{','.join(audio_filters)}"
            + ("[abase]" if sfx_index is not None else "[a]")
        )
    else:
        chains.append(
            f"[0:a:0]{','.join(audio_filters)}"
            + ("[abase]" if sfx_index is not None else "[a]")
        )

    if sfx_index is not None:
        chains.append(
            f"[{sfx_index}:a]adelay={int(sfx['at'] * 1000)}:all=1,volume={sfx['gain_db']}dB[sfx]"
        )
        # duration=first: the effect never extends the cut it decorates.
        chains.append("[abase][sfx]amix=inputs=2:normalize=0:duration=first[a]")

    return (
        head + extra + ["-filter_complex", ";".join(chains)] + tail
        + ["-map", video_label, "-map", "[a]", out_path]
    )


def _graphic_input(effect: dict[str, Any]) -> dict[str, Any] | None:
    """One overlaid image for this cut (10.2 그래픽).

    The plan states ``"graphic": "path.png"`` or a dict with ``path`` and any of
    ``x``/``y`` (ffmpeg overlay expressions, default centred), ``width`` (pixels,
    height follows the aspect) and ``start``/``end`` seconds within the cut.
    A path that is not there is dropped with a warning rather than failing the
    whole render for one decoration.
    """
    graphic = effect.get("graphic")
    if not graphic:
        return None
    spec = {"path": graphic} if isinstance(graphic, str) else dict(graphic)
    path = str(spec.get("path", ""))
    if not path or not Path(path).is_file():
        log.warning("graphic %r is not a file; skipping the overlay", path)
        return None
    return {
        "path": path,
        "x": spec.get("x", "(W-w)/2"),
        "y": spec.get("y", "(H-h)/2"),
        "width": int(spec["width"]) if spec.get("width") else 0,
        "start": float(spec.get("start", 0.0)),
        "end": float(spec.get("end", 1e9)),
    }


def _sfx_input(audio_effect: dict[str, Any]) -> dict[str, Any] | None:
    """One sound effect mixed into this cut (10.2 효과음).

    ``"sfx": "path.wav"`` or a dict with ``path``, ``at`` seconds into the cut,
    and ``gain_db``. Like the graphic, a missing file is skipped rather than
    fatal - 10.1 says the renderer makes no decisions, and dying over a missing
    decoration would throw away the whole cut it belonged to.
    """
    sfx = audio_effect.get("sfx")
    if not sfx:
        return None
    spec = {"path": sfx} if isinstance(sfx, str) else dict(sfx)
    path = str(spec.get("path", ""))
    if not path or not Path(path).is_file():
        log.warning("sound effect %r is not a file; skipping it", path)
        return None
    return {
        "path": path,
        "at": max(0.0, float(spec.get("at", 0.0))),
        "gain_db": float(spec.get("gain_db", 0.0)),
    }


def _count_audio_streams(source: str) -> int:
    """How many audio streams the source carries, for the mix above.

    Probed rather than configured: 17.1 externalises judgement thresholds, and
    this is not one — it is a fact about the file. A probe that fails falls back
    to one stream, which is the old behaviour and cannot make the render worse
    than it already was.
    """
    from aicut.media.probe import probe

    try:
        return max(1, len(probe(source).audio_tracks))
    except Exception as exc:                       # ffprobe missing or unhappy
        log.warning("could not count audio streams in %s (%s); mixing skipped", source, exc)
        return 1


def concat_entry(path: str | Path) -> str:
    """One line of the concat demuxer's manifest, with the path escaped.

    The demuxer parses quotes as syntax, so a path containing an apostrophe -
    `/home/O'Brien/aicut` - ended the quoted string early and ffmpeg went
    looking for `/home/OBrien/...`, which does not exist. Measured: every
    segment encodes, and only the join fails, so the whole render is lost at
    the last step.

    Single quotes cannot be escaped inside single quotes; the quoting has to be
    closed, an escaped quote emitted, and the quoting reopened - `'\''`.
    """
    return "file '" + str(path).replace("'", "'\\''") + "'"


def build_concat_command(list_path: str, out_path: str) -> list[str]:
    """Join the segments (10.4-2: concat, not a tower of crossfades)."""
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy", out_path,
    ]


def _escape_filter_path(path: str) -> str:
    """Make a path safe inside an ffmpeg filter argument.

    Filter syntax treats ':' as an option separator and quotes as delimiters,
    so a Windows path like ``C:\\work\\subs.ass`` has to arrive as
    ``C\\:/work/subs.ass`` - backslashes turned into forward slashes, the drive
    colon escaped. On POSIX the same rules are harmless. An unescaped path does
    not error; it produces a video with no captions, which is worse.
    """
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_final_command(
    joined_path: str,
    out_path: str,
    settings: RenderSettings,
    *,
    ass_path: str | None = None,
    loudness: LoudnessStats | None = None,
    fonts_dir: str | None = None,
    bgm: dict[str, Any] | None = None,
) -> list[str]:
    """Burn subtitles, lay the BGM bed, and apply the loudness correction.

    10.2 lists BGM among the renderer's features and 10.4-3 says the EBU R128
    correction is applied after a 2-pass measurement. The bed goes in here
    rather than per segment because it runs under the whole timeline: mixing it
    into each cut would restart it at every join, and the measurement would
    then be of a timeline the bed had not been added to yet.
    """
    video_filters: list[str] = []
    if ass_path:
        # `filename=` is named rather than passed positionally: ffmpeg 7.2 (the
        # homebrew build) rejects `subtitles='<path>'` with "No option name
        # near", where 6.x and 7.1 accepted it. Naming the option works on all
        # of them, and the failure it avoids is a render that dies rather than
        # one that quietly ships without captions.
        subtitle = f"subtitles=filename='{_escape_filter_path(ass_path)}'"
        if fonts_dir:
            subtitle += f":fontsdir='{_escape_filter_path(fonts_dir)}'"
        video_filters.append(subtitle)

    loudnorm = f"loudnorm=I={settings.loudness_i}:TP={settings.loudness_tp}:LRA={settings.loudness_lra}"
    if loudness is not None:
        loudnorm += (
            f":measured_I={loudness.input_i}:measured_TP={loudness.input_tp}"
            f":measured_LRA={loudness.input_lra}:measured_thresh={loudness.input_thresh}"
            f":offset={loudness.target_offset}:linear=true"
        )
    bed = _bgm_input(bgm)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", joined_path]
    if bed:
        cmd += _bed_input_args(bed)
    if video_filters:
        cmd += ["-vf", ",".join(video_filters), "-c:v", settings.video_codec,
                "-preset", settings.preset, "-crf", str(settings.crf), "-pix_fmt", settings.pix_fmt]
    else:
        cmd += ["-c:v", "copy"]
    audio_chain = f"{loudnorm},aresample={settings.sample_rate}"
    if bed:
        # The bed is levelled and faded, mixed under the dialogue, and only then
        # corrected - so loudnorm sees the timeline a viewer actually hears.
        # measure_loudness_with_bed() below builds the same mix, so the
        # measurement describes this filter's real input (10.4-3).
        cmd += ["-filter_complex", ";".join(_bed_mix_chains(bed, audio_chain)),
                "-map", "0:v:0", "-map", "[a]"]
    else:
        cmd += ["-af", audio_chain]
    cmd += [
        "-c:a", settings.audio_codec, "-b:a", settings.audio_bitrate, "-ar", str(settings.sample_rate),
        "-movflags", "+faststart",
        out_path,
    ]
    return cmd


def _bed_input_args(bed: dict[str, Any]) -> list[str]:
    """The BGM file as a second input.

    -stream_loop goes before the input it applies to; -1 loops a short bed under
    a long timeline, and duration=first in the mix stops it outrunning the video.
    """
    return ["-stream_loop", "-1" if bed["loop"] else "0", "-i", bed["path"]]


def _bed_mix_chains(bed: dict[str, Any], tail: str) -> list[str]:
    """The filter graph that lays the bed under the dialogue, ending in [a].

    One function, two callers: the render pass and the loudness measurement.
    They have to agree exactly - a measurement of a different mix is worse than
    no measurement, because it is applied as though it were right.
    """
    return [
        f"[1:a]volume={bed['gain_db']}dB,afade=t=in:st=0:d={bed['fade']:.3f}[bed]",
        f"[0:a][bed]amix=inputs=2:normalize=0:duration=first{',' + tail if tail else ''}[a]",
    ]


def measure_loudness_with_bed(
    joined_path: str, settings: RenderSettings, profile: CalibrationProfile,
    bgm: dict[str, Any] | None,
) -> LoudnessStats | None:
    """First pass of 10.4-3, over the mix the second pass will actually correct.

    Without a bed this is `measure_loudness` unchanged. With one, the bed is
    mixed in first: measuring the bare dialogue and then correcting a timeline
    that has music under it means the advertised two-pass normalisation misses
    its LUFS target and can clip on a loud bed.
    """
    # The correcting pass reads its targets off the settings, which a stored
    # plan can carry (8.2) - so measuring against the profile's numbers instead
    # applied an offset computed for a target nobody was aiming at.
    targets = (settings.loudness_i, settings.loudness_tp, settings.loudness_lra)
    bed = _bgm_input(bgm)
    if bed is None:
        return measure_loudness(joined_path, profile, targets=targets)

    require_ffmpeg()
    target_i, target_tp, target_lra = targets
    loudnorm = (
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    )
    output = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", joined_path]
        + _bed_input_args(bed)
        + ["-filter_complex", ";".join(_bed_mix_chains(bed, loudnorm)),
           "-map", "[a]", "-f", "null", "-"]
    )
    return parse_loudnorm_json(output)


def _bgm_input(bgm: dict[str, Any] | None) -> dict[str, Any] | None:
    """The music bed under the timeline (10.2 BGM).

    The plan states ``"bgm": "path.mp3"`` or a dict with ``path``, ``gain_db``
    (default well under the voice), ``fade`` seconds and ``loop``. Missing file
    means no bed and a warning, not a failed render.
    """
    if not bgm:
        return None
    spec = {"path": bgm} if isinstance(bgm, str) else dict(bgm)
    path = str(spec.get("path", ""))
    if not path or not Path(path).is_file():
        log.warning("bgm %r is not a file; rendering without a music bed", path)
        return None
    return {
        "path": path,
        # A bed sits under speech. The plan can say otherwise, but silence here
        # would mean whatever level the file happens to have, over the voice.
        "gain_db": float(spec.get("gain_db", -18.0)),
        "fade": max(0.0, float(spec.get("fade", 1.0))),
        "loop": bool(spec.get("loop", True)),
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def _pieces_per_cut(segments: Sequence[Segment]) -> dict[int, list[Segment]]:
    """The segments each cut was split into, in output order."""
    grouped: dict[int, list[Segment]] = {}
    for segment in segments:
        grouped.setdefault(segment.sequence_order, []).append(segment)
    for pieces in grouped.values():
        pieces.sort(key=lambda s: s.source_start_sec)
    return grouped


def _effects_for_piece(
    cut, segment: Segment, pieces: dict[int, list[Segment]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One cut's 8.2 intent, scoped to the piece of it being rendered.

    An uncut cut is one piece and gets everything. A cut pacing has split is
    several, and the intent has to be placed rather than copied:

    * a transition belongs to the join, so its ``in`` goes on the first piece
      and its ``out`` on the last - a fade at every internal removal is not
      what the plan asked for;
    * a graphic and a sound effect happen once, at a time measured from the
      start of the cut, so they go on the piece that actually contains that
      time, with the offset rebased onto that piece;
    * zoom and crop describe the framing of the whole cut, so every piece keeps
      them.
    """
    visual = dict(cut.visual_effect or {}) if cut else {}
    audio = dict(cut.audio_effect or {}) if cut else {}
    group = pieces.get(segment.sequence_order, [segment])
    if len(group) < 2:
        return visual, audio

    first, last = group[0], group[-1]
    transition = visual.get("transition")
    if transition:
        if isinstance(transition, str):
            transition = {"in": transition, "out": transition}
        else:
            transition = dict(transition)
        if segment is not first:
            transition.pop("in", None)
        if segment is not last:
            transition.pop("out", None)
        if any(transition.get(edge) for edge in ("in", "out")):
            visual["transition"] = transition
        else:
            visual.pop("transition", None)

    # Where this piece begins on the cut's own clock. The plan states a
    # graphic's start/end and a sound effect's `at` in seconds within the cut,
    # and it was written before pacing removed anything - so the offset is the
    # piece's position in the SOURCE, not the sum of the surviving pieces before
    # it. Measured: on a 0-20s cut with 10-12s removed, an effect at 14s belongs
    # 2s into the piece starting at source 12s; summing durations put it at 4s,
    # and a larger removal moves it onto the wrong piece or off the end.
    offset = segment.source_start_sec - cut.source_start_sec
    scope_timed_effects(visual, audio, offset=offset, duration=segment.duration)
    return visual, audio


def scope_timed_effects(
    visual: dict[str, Any], audio: dict[str, Any], *, offset: float, duration: float,
) -> None:
    """Move a graphic and a sound effect onto the piece that contains them.

    ``offset`` is where this piece begins on the clock the times are written on,
    and ``duration`` how long it runs. A graphic that ends before the piece
    starts, or a sound effect timed after it ends, does not belong here at all
    and is removed rather than clamped to the edge.

    Both callers split one continuous stretch of video into pieces: pacing
    removing something out of the middle of a cut, and 10.4-1 (a) cutting a cut
    at its zoom keyframes. Either way an 8.2 time written for the whole stretch
    is meaningless on a piece until it is rebased.
    """
    span = (offset, offset + duration)

    graphic = visual.get("graphic")
    if graphic:
        spec = {"path": graphic} if isinstance(graphic, str) else dict(graphic)
        start = float(spec.get("start", 0.0))
        end = float(spec.get("end", offset + duration))
        if end <= span[0] or start >= span[1]:
            visual.pop("graphic", None)
        else:
            spec["start"] = max(0.0, start - offset)
            spec["end"] = min(duration, end - offset)
            visual["graphic"] = spec

    sfx = audio.get("sfx")
    if sfx:
        spec = {"path": sfx} if isinstance(sfx, str) else dict(sfx)
        at = float(spec.get("at", 0.0))
        if span[0] <= at < span[1]:
            spec["at"] = at - offset
            audio["sfx"] = spec
        else:
            audio.pop("sfx", None)


#: Only for a caller with no profile. How short a framing step may be is a
#: judgement about camera work - 17.1 keeps those in the profile, under
#: `render.zoom.min_piece_sec`.
DEFAULT_MIN_ZOOM_PIECE_SEC = 0.4


def zoom_pieces(
    segment: Segment, keyframes: Sequence[dict[str, Any]],
    *, min_piece_sec: float = DEFAULT_MIN_ZOOM_PIECE_SEC,
) -> list[tuple[Segment, dict[str, Any]]]:
    """Split one segment at its zoom keyframes, strategy (a) of 10.4-1.

    10.4-1 (a) is "줌 구간을 세그먼트로 분리하고 세그먼트별 고정 crop 적용 후
    concat" - a stepped camera, and the one strategy that can actually change
    magnification (see :func:`sendcmd_file` for why sendcmd cannot). Without
    this the keyframes were read only by the sendcmd path: under
    ``segment_crop`` a plan carrying a moving zoom rendered as one static crop
    and nothing said the movement had been dropped.

    Keyframe times are on the segment's own clock. Each piece runs from its
    keyframe to the next and holds that keyframe's framing.

    ``min_piece_sec`` is how short a framing step may be before it is not worth
    cutting: ffmpeg's keyframe seek plus the concat join costs more than the
    change is worth, and a quarter-second piece reads as a glitch rather than a
    camera move. It comes from the profile (17.1).
    """
    ordered = sorted(keyframes, key=lambda k: float(k.get("at_sec", 0.0)))
    if len(ordered) < 2:
        return []
    bounds: list[tuple[float, dict[str, Any]]] = []
    for keyframe in ordered:
        at = max(0.0, min(segment.duration, float(keyframe.get("at_sec", 0.0))))
        if bounds and at - bounds[-1][0] < min_piece_sec:
            continue
        bounds.append((at, keyframe))
    if not bounds:
        return []
    # The first piece starts at the segment's start whatever the first keyframe
    # says: dropping the head would shorten the cut the plan asked for.
    bounds[0] = (0.0, bounds[0][1])
    if len(bounds) < 2:
        return []
    if segment.duration - bounds[-1][0] < min_piece_sec:
        bounds.pop()
    if len(bounds) < 2:
        return []

    pieces: list[tuple[Segment, dict[str, Any]]] = []
    for index, (at, keyframe) in enumerate(bounds):
        end = bounds[index + 1][0] if index + 1 < len(bounds) else segment.duration
        pieces.append((
            replace(
                segment,
                source_start_sec=segment.source_start_sec + at,
                source_end_sec=segment.source_start_sec + end,
                out_start_sec=segment.out_start_sec + at,
            ),
            {
                "scale": float(keyframe.get("scale", 0.83)),
                "center": list(keyframe.get("center", [0.5, 0.5])),
            },
        ))
    return pieces


def _directory_mb(path: Path) -> float:
    try:
        return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 1e6
    except OSError:
        return 0.0


class Renderer:
    """Executes an edit plan. Contains no decision of any kind."""

    def __init__(self, profile: CalibrationProfile, work_dir: str | Path):
        self.profile = profile
        self.work_dir = Path(work_dir)

    def render(
        self,
        plan: EditPlan,
        out_path: str | Path,
        *,
        ass_path: str | Path | None = None,
        keep_intermediate: bool = False,
    ) -> Path:
        require_ffmpeg()
        if ass_path:
            # Checked here rather than at the final command: by then the
            # segments have been cut and joined, and the failure reads as a
            # mysterious render error instead of a missing build option.
            require_filter(
                "subtitles", needed_for="burning subtitles (10.3)", install_hint=LIBASS_HINT
            )
        settings = RenderSettings.from_profile(
            self.profile, target_type=plan.target_type,
        ).with_plan(plan.render_settings)
        audio_streams = _count_audio_streams(plan.source_path)
        timeline = Timeline.from_cuts(plan.cuts)
        if not timeline.segments:
            raise RenderError(f"episode {plan.episode_id} has no renderable segment")

        # Absolute, because ffmpeg's concat demuxer resolves the paths in the
        # list file relative to the list file's own directory: a relative path
        # written there would be joined onto the stage directory twice.
        stage = (self.work_dir / plan.episode_id).resolve()
        stage.mkdir(parents=True, exist_ok=True)
        cuts_by_order = {c.sequence_order: c for c in plan.cuts}

        # Pacing splits one cut into several segments when it removes a span
        # from inside it, and a cut's editing intent (8.2) belongs to the cut,
        # not to each piece. Giving every piece the full intent made a
        # transition fade at every internal silence removal and restarted the
        # graphic and the sound effect on each surviving piece.
        pieces = _pieces_per_cut(timeline.segments)

        segment_paths: list[Path] = []
        for i, segment in enumerate(timeline.segments):
            cut = cuts_by_order.get(segment.sequence_order)
            seg_path = stage / f"seg_{i:05d}.mp4"
            sendcmd = None
            visual, audio = _effects_for_piece(cut, segment, pieces)
            keyframes = visual.get("keyframes") if visual.get("type") == "zoom" else None
            if keyframes and settings.zoom_strategy == "sendcmd":
                sendcmd = str(sendcmd_file(keyframes, stage / f"seg_{i:05d}.cmd"))
            elif keyframes:
                # Strategy (a) of 10.4-1: one fixed crop per keyframe, joined.
                # Without this branch the keyframes were dropped in silence and
                # the camera stood still.
                zoomed = zoom_pieces(
                    segment, keyframes,
                    min_piece_sec=self.profile.get_float("render.zoom.min_piece_sec"),
                )
                if zoomed:
                    for part, (piece, framing) in enumerate(zoomed):
                        piece_path = stage / f"seg_{i:05d}_{part:03d}.mp4"
                        piece_visual = dict(visual)
                        piece_visual.pop("keyframes", None)
                        piece_visual.update(framing)
                        piece_audio = dict(audio)
                        # The graphic and the sound effect happen once, at a
                        # time measured on the whole cut. Leaving them on the
                        # first piece staged them at that time inside a piece
                        # that may only be a second long: a graphic timed at 5s
                        # never appeared, and the sound effect fired at the top
                        # of the camera move instead of where the plan put it.
                        scope_timed_effects(
                            piece_visual, piece_audio,
                            offset=piece.source_start_sec - segment.source_start_sec,
                            duration=piece.duration,
                        )
                        run(build_segment_command(
                            plan.source_path, piece, str(piece_path), settings,
                            visual_effect=piece_visual,
                            audio_effect=piece_audio,
                            audio_streams=audio_streams,
                        ))
                        segment_paths.append(piece_path)
                    continue
            run(build_segment_command(
                plan.source_path, segment, str(seg_path), settings,
                visual_effect=visual,
                audio_effect=audio,
                sendcmd_path=sendcmd,
                audio_streams=audio_streams,
            ))
            segment_paths.append(seg_path)

        list_path = stage / "segments.txt"
        list_path.write_text(
            "\n".join(concat_entry(p.as_posix()) for p in segment_paths) + "\n",
            encoding="utf-8",
        )
        joined = stage / "joined.mp4"
        run(build_concat_command(str(list_path), str(joined)))

        loudness = None
        if settings.two_pass_loudness:
            loudness = measure_loudness_with_bed(
                str(joined), settings, self.profile, plan.structure.get("bgm"),
            )

        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            run(build_final_command(
                str(joined), str(target), settings,
                ass_path=str(ass_path) if ass_path else None,
                loudness=loudness,
                # 10.2 BGM. It belongs to the episode, not to any one cut, so
                # the structure is where the plan states it (8.2).
                bgm=plan.structure.get("bgm"),
            ))
        except RenderError:
            # The cut segments stay, because they are what a person needs to see
            # to work out why this failed - but silently leaving hundreds of
            # megabytes behind is how a desktop program (22.1) fills a disk
            # without anyone knowing which directory did it.
            log.warning(
                "render failed; leaving %s in place for inspection (%.0f MB) - "
                "delete it once the cause is found",
                stage, _directory_mb(stage),
            )
            raise

        if not keep_intermediate:
            shutil.rmtree(stage, ignore_errors=True)
        return target
