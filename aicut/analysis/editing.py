"""How a finished video was cut (4.3).

4.3 asks what the editing of a reference video is: 컷 / 평균 장면 길이 / 확대 /
크롭 / 화면 전환 / 자막 / 강조 / 효과 / 효과음 / BGM / 이미지 / 밈 / 리플레이.

The split follows 18장. The program measures what is countable from the file —
where the cuts are, how long the shots run, how the rhythm changes across the
video. Everything else is a judgement about what is on screen, and that is the
AI's, made from the frames themselves; nothing here tries to decide it.

The same measurement serves both sides of the plan:

* 4장 / 12.3 A — a reference video someone else made. 4.6 allows this as long
  as only the analysis is kept: the caller hands over a file, this reads it,
  and the media is discarded. There is no column to store it in.
* 12.3 B — the operator's own finished video. Loop B aligns speech between the
  source and the output, which cannot see a caption, an effect or a cut that
  falls inside one sentence. The cut rhythm can.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from statistics import median
from typing import Sequence

from aicut.config import CalibrationProfile
from aicut.media.vision import MotionSample

log = logging.getLogger(__name__)


@dataclass
class Cut:
    """One shot boundary, and how hard it was."""

    at_sec: float
    score: float


@dataclass
class EditFingerprint:
    """What the file says about its own cutting. No judgement, only counts."""

    duration_sec: float
    cuts: list[Cut] = field(default_factory=list)
    shot_lengths_sec: list[float] = field(default_factory=list)

    @property
    def cut_count(self) -> int:
        return len(self.cuts)

    @property
    def cuts_per_minute(self) -> float:
        return self.cut_count / (self.duration_sec / 60) if self.duration_sec > 0 else 0.0

    @property
    def mean_shot_sec(self) -> float:
        return sum(self.shot_lengths_sec) / len(self.shot_lengths_sec) if self.shot_lengths_sec else 0.0

    @property
    def median_shot_sec(self) -> float:
        return median(self.shot_lengths_sec) if self.shot_lengths_sec else 0.0

    @property
    def shortest_shot_sec(self) -> float:
        return min(self.shot_lengths_sec) if self.shot_lengths_sec else 0.0

    @property
    def longest_shot_sec(self) -> float:
        return max(self.shot_lengths_sec) if self.shot_lengths_sec else 0.0

    def pace_over_time(self, buckets: int = 6) -> list[float]:
        """Cuts per minute across equal slices of the video.

        4.3 asks about 영상 템포 and 정보 공개 순서 — a video that opens fast and
        settles is a different edit from one that builds, and a single average
        cannot tell them apart.
        """
        if buckets <= 0 or self.duration_sec <= 0:
            return []
        span = self.duration_sec / buckets
        minutes = span / 60
        counts = [0] * buckets
        for cut in self.cuts:
            index = min(buckets - 1, int(cut.at_sec / span))
            counts[index] += 1
        return [round(count / minutes, 2) if minutes else 0.0 for count in counts]

    def to_dict(self) -> dict:
        return {
            "duration_sec": round(self.duration_sec, 2),
            "cut_count": self.cut_count,
            "cuts_per_minute": round(self.cuts_per_minute, 2),
            "mean_shot_sec": round(self.mean_shot_sec, 2),
            "median_shot_sec": round(self.median_shot_sec, 2),
            "shortest_shot_sec": round(self.shortest_shot_sec, 2),
            "longest_shot_sec": round(self.longest_shot_sec, 2),
            "pace_over_time": self.pace_over_time(),
            "cuts_sec": [round(c.at_sec, 2) for c in self.cuts],
        }


def detect_cuts(samples: Sequence[MotionSample], profile: CalibrationProfile) -> list[Cut]:
    """Shot boundaries, from the scene score already measured for the source.

    A cut shows up as one sample whose scene score jumps well above the ones
    around it. Thresholding the raw score alone does not survive contact with
    real footage — a bright game scene sits high all the way through and every
    sample clears a fixed bar — so a sample has to stand out from its own
    neighbourhood as well.

    Both numbers are profile values (17.1). A channel that plays one game has a
    different baseline from one that switches every stream, and 17.4 is where
    that gets settled rather than here.
    """
    if not samples:
        return []
    floor = profile.get_float("editing.cut_score_floor")
    ratio = profile.get_float("editing.cut_local_ratio")
    window = profile.get_int("editing.cut_local_window")

    ordered = sorted(samples, key=lambda s: s.at_sec)
    cuts: list[Cut] = []
    for index, sample in enumerate(ordered):
        if sample.score < floor:
            continue
        low = max(0, index - window)
        high = min(len(ordered), index + window + 1)
        neighbours = [s.score for i, s in enumerate(ordered[low:high], start=low) if i != index]
        if not neighbours:
            cuts.append(Cut(sample.at_sec, sample.score))
            continue
        local = sum(neighbours) / len(neighbours)
        # A flat-but-high stretch is a busy picture, not a cut every second.
        if local <= 0 or sample.score >= local * ratio:
            cuts.append(Cut(sample.at_sec, sample.score))
    return cuts


def fingerprint(
    samples: Sequence[MotionSample], duration_sec: float, profile: CalibrationProfile,
) -> EditFingerprint:
    """Measure the cutting of one finished video."""
    cuts = detect_cuts(samples, profile)
    boundaries = [0.0] + [c.at_sec for c in cuts] + [duration_sec]
    lengths = [
        round(end - start, 3)
        for start, end in zip(boundaries, boundaries[1:])
        if end > start
    ]
    return EditFingerprint(duration_sec=duration_sec, cuts=list(cuts), shot_lengths_sec=lengths)
