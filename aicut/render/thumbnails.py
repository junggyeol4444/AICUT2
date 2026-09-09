"""Thumbnail candidates (11.1).

Frames are scored inside the *finished* video, not the source, because a
thumbnail promises what the video contains. The three signals - audio tension,
change in expression, something happening on screen - are weighted by the
profile, since which of them matters is exactly the kind of thing 17.4 has to
measure per channel rather than assume.

No template is applied. The system extracts the frames and the human picks
(15.5); overlaying a fixed layout would be the same hardcoding 2장 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from aicut.analysis.tension import TensionCurve
from aicut.media import faces as faces_mod
from aicut.config import CalibrationProfile
from aicut.media.ffmpeg_util import require_ffmpeg, run
from aicut.media.vision import MotionSample


@dataclass
class ThumbnailCandidate:
    at_sec: float
    score: float
    path: str = ""
    signals: dict[str, float] | None = None


def score_frames(
    duration_sec: float,
    tension: TensionCurve,
    motion: list[MotionSample],
    profile: CalibrationProfile,
    *,
    step_sec: float = 1.0,
    faces: Sequence[Any] = (),
) -> list[ThumbnailCandidate]:
    """Rank moments of the finished video as thumbnail sources.

    11.1 names three signals: 오디오 텐션 / 표정 변화 폭 / 화면 사건 발생 여부.
    They are three, not two dressed as three - the face moving and the screen
    changing are different events, and a thumbnail wants the first. When face
    readings are supplied the expression signal comes from them; without a
    detector it is absent from the signal set rather than filled in with the
    frame delta, because a weight applied to a stand-in is a weight 17장 cannot
    calibrate.
    """
    weights = profile.get("thumbnail.weights")
    min_gap = profile.get_float("thumbnail.min_gap_sec")
    count = profile.get_int("thumbnail.candidate_count")
    face_window = profile.get_float("thumbnail.expression_window_sec")

    motion_by_sec = {round(m.at_sec): m.score for m in motion}
    scored: list[ThumbnailCandidate] = []
    at = 0.0
    while at < duration_sec:
        signals: dict[str, float] = {
            "audio_tension": tension.at(at),
            "screen_event": motion_by_sec.get(round(at), 0.0),
        }
        if faces:
            signals["expression_change"] = faces_mod.expression_change(
                faces, at, window_sec=face_window,
            )
        score = sum(float(weights.get(k, 0.0)) * v for k, v in signals.items())
        scored.append(ThumbnailCandidate(at_sec=at, score=score, signals=signals))
        at += step_sec

    scored.sort(key=lambda c: c.score, reverse=True)
    picked: list[ThumbnailCandidate] = []
    for candidate in scored:
        if all(abs(candidate.at_sec - p.at_sec) >= min_gap for p in picked):
            picked.append(candidate)
        if len(picked) >= count:
            break
    return picked


def extract(video_path: str, candidates: list[ThumbnailCandidate], out_dir: str | Path) -> list[ThumbnailCandidate]:
    """Pull each candidate frame out at full quality (11.1)."""
    require_ffmpeg()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for i, candidate in enumerate(candidates):
        path = out / f"thumb_{i:02d}_{int(candidate.at_sec):06d}.png"
        run([
            "ffmpeg", "-hide_banner", "-nostats", "-y",
            "-ss", f"{candidate.at_sec:.3f}", "-i", video_path,
            "-frames:v", "1", "-q:v", "1", str(path),
        ])
        candidate.path = str(path)
    return candidates
