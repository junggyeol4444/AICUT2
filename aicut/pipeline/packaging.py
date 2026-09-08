"""PACKAGED: thumbnails and metadata for a finished video (11장).

Nothing here is templated. Thumbnail frames are scored out of the finished video
and offered as candidates for a person to choose from (11.1, 15.5); titles,
description, tags and chapters are written for this video's content, informed by
the reference patterns of 4.5 rather than filled into a fixed form (11.2).
"""

from __future__ import annotations

import json
import logging

from aicut.analysis.tension import TensionCurve, build_tension_curve
from aicut.media import audio as audio_mod
from aicut.media import vision as vision_mod
from aicut.media.ffmpeg_util import have_ffmpeg
from aicut.models import Episode
from aicut.pipeline.context import RunContext
from aicut.media import faces as faces_mod
from aicut.render import thumbnails
from aicut.render.timeline import Timeline

log = logging.getLogger(__name__)


def run(ctx: RunContext, episodes: list[Episode], *, knowledge: dict | None = None) -> list[Episode]:
    for episode in episodes:
        package_episode(ctx, episode, knowledge=knowledge)
    ctx.note("episodes_packaged", len(episodes))
    return episodes


def package_episode(ctx: RunContext, episode: Episode, *, knowledge: dict | None = None) -> Episode:
    timeline = Timeline.from_cuts(episode.timeline)
    boundaries = timeline.cut_boundaries()
    candidates = ctx.store.candidates(ctx.project.project_id)
    core = " / ".join(c.core_summary for c in candidates if c.candidate_id in episode.candidate_ids)

    if episode.output_mp4_path and have_ffmpeg():
        episode.thumbnail_candidates = [c.path for c in _thumbnails(ctx, episode)]

    answer = ctx.producer.package_metadata({
        "core_summary": core,
        "structure": episode.planned_structure,
        "target_type": episode.target_type,
        "duration_sec": timeline.duration,
        "cuts": [
            {
                "sequence_order": c.sequence_order,
                "scene_role": c.scene_role,
                "speaker": c.speaker_tag,
                # Where this cut begins in the finished video - the only places a
                # chapter mark can honestly sit.
                "output_start_sec": round(at, 2),
            }
            for c, at in zip(sorted(episode.timeline, key=lambda c: c.sequence_order), boundaries)
        ],
        "subtitles": [{"at_sec": s.start_sec, "text": s.text} for s in episode.subtitles[:200]],
        "youtube_knowledge": knowledge or {},
    })

    titles = [t for t in (answer.get("titles") or []) if t][:3]
    episode.title_candidates = titles
    episode.metadata = {
        "titles": titles,
        "description": answer.get("description", ""),
        "tags": answer.get("tags", []) or [],
        "chapters": answer.get("chapters", []) or [],
        # 원본 24장 lists 업로드 정보 alongside the rest of the package. Privacy
        # is deliberately not in here: 11.3 makes that the profile's and the
        # reviewer's, never the model's.
        "upload": {k: v for k, v in (answer.get("upload") or {}).items()
                   if k in ("category_id", "language", "playlist") and v},
        "duration_sec": round(timeline.duration, 2),
    }
    path = ctx.project_dir / "metadata" / f"{episode.episode_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(episode.metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    # 22.4 asks for the package as JSON·TXT. The JSON is for the uploader; the
    # text is for the person, who has to paste a description into a box and
    # cannot do that from a file full of escapes.
    path.with_suffix(".txt").write_text(_as_text(episode), encoding="utf-8")

    ctx.store.save_episode(episode)
    return episode


def _as_text(episode: Episode) -> str:
    """The metadata package as something a person can copy out of (22.4)."""
    meta = episode.metadata
    lines = ["제목 후보"]
    lines += [f"  {i}. {t}" for i, t in enumerate(meta.get("titles", []), 1)] or ["  (없음)"]
    lines += ["", "설명", meta.get("description", "") or "(없음)"]
    chapters = meta.get("chapters", [])
    if chapters:
        lines += ["", "챕터"]
        for chapter in chapters:
            at = int(float(chapter.get("at_sec", 0)))
            stamp = f"{at // 3600:d}:{at // 60 % 60:02d}:{at % 60:02d}" if at >= 3600 \
                else f"{at // 60:d}:{at % 60:02d}"
            lines.append(f"  {stamp} {chapter.get('label', '')}")
    tags = meta.get("tags", [])
    if tags:
        lines += ["", "태그", "  " + ", ".join(tags)]
    upload = meta.get("upload") or {}
    if upload:
        lines += ["", "업로드 정보"]
        lines += [f"  {k}: {v}" for k, v in upload.items()]
    return "\n".join(lines) + "\n"


def _thumbnails(ctx: RunContext, episode: Episode) -> list[thumbnails.ThumbnailCandidate]:
    """Score the finished file, then extract the top frames at full quality."""
    video = episode.output_mp4_path
    try:
        rms = audio_mod.rms_envelope(video)
        motion = vision_mod.motion_curve(video, interval_sec=1.0)
    except Exception as exc:                     # measurement is optional, the video is not
        log.warning("thumbnail scoring skipped for %s: %s", episode.episode_id, exc)
        return []
    curve: TensionCurve = build_tension_curve(rms, [], ctx.profile)
    duration = curve.times[-1] if curve.times else 0.0
    # 11.1's 표정 변화 폭 needs a face, so the finished cut is read for one.
    # Optional, like every other visual stage: without a detector the signal is
    # simply absent (see score_frames).
    faces: list = []
    detector = faces_mod.build_detector()
    if detector is not None:
        try:
            frames = vision_mod.sample_frames(
                video, ctx.project_dir / "thumbnails" / episode.episode_id / "scan",
                start_sec=0.0, duration_sec=duration,
                # Same second-by-second grid the motion curve above was measured
                # on, so a face reading lines up with a screen_event reading.
                interval_sec=1.0, prefix="thumb",
            )
            faces = detector.read_frames([(f.at_sec, f.path) for f in frames])
        except Exception as exc:
            log.warning("thumbnail face scan skipped for %s: %s", episode.episode_id, exc)
    picked = thumbnails.score_frames(duration, curve, motion, ctx.profile, faces=faces)
    return thumbnails.extract(video, picked, ctx.project_dir / "thumbnails" / episode.episode_id)
