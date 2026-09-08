"""Evaluation metrics for calibration (17.3).

Three questions, taken verbatim from the document:

* **pacing accuracy** - of the silences a person kept, how many did the system
  keep (recall), and of the silences a person cut, how many did the system cut
  (precision). 9.4 makes this mandatory rather than optional: pacing is the most
  subjective judgement in the system, so it is scored against a real human edit
  or not trusted at all.
* **content discovery agreement** - did the system find the material a person
  would call content?
* **false positive rate** - did it promote something a person would throw away?
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from aicut.config import CalibrationProfile

Span = tuple[float, float]

#: Used only when no profile is passed - a caller scoring two lists of spans
#: outside a calibration run. Every path that has a profile reads it from there,
#: because these decide which profile a sweep picks (17.1).
DEFAULT_MIN_IOU = 0.3
DEFAULT_SCORE_WEIGHTS = {"discovery": 0.6, "pacing": 0.4}


def overlap(a: Span, b: Span) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def iou(a: Span, b: Span) -> float:
    union = (a[1] - a[0]) + (b[1] - b[0]) - overlap(a, b)
    return overlap(a, b) / union if union > 0 else 0.0


@dataclass
class PacingScore:
    keep_recall: float          # human kept it, system kept it
    cut_precision: float        # system cut it, human also cut it
    accuracy: float
    kept_by_human: int
    kept_by_system: int
    total: int

    @property
    def f1(self) -> float:
        if self.keep_recall + self.cut_precision == 0:
            return 0.0
        return 2 * self.keep_recall * self.cut_precision / (self.keep_recall + self.cut_precision)


def score_pacing(system_keeps: Sequence[bool], human_keeps: Sequence[bool]) -> PacingScore:
    """Compare per-silence verdicts, aligned index by index."""
    if len(system_keeps) != len(human_keeps):
        raise ValueError("pacing scoring needs one system verdict per human verdict")
    total = len(human_keeps)
    if total == 0:
        return PacingScore(0.0, 0.0, 0.0, 0, 0, 0)

    human_kept = [i for i, k in enumerate(human_keeps) if k]
    system_cut = [i for i, k in enumerate(system_keeps) if not k]
    keep_recall = (
        sum(1 for i in human_kept if system_keeps[i]) / len(human_kept) if human_kept else 1.0
    )
    cut_precision = (
        sum(1 for i in system_cut if not human_keeps[i]) / len(system_cut) if system_cut else 1.0
    )
    accuracy = sum(1 for s, h in zip(system_keeps, human_keeps) if s == h) / total
    return PacingScore(
        keep_recall=round(keep_recall, 4),
        cut_precision=round(cut_precision, 4),
        accuracy=round(accuracy, 4),
        kept_by_human=len(human_kept),
        kept_by_system=sum(1 for k in system_keeps if k),
        total=total,
    )


@dataclass
class ContentDiscoveryScore:
    recall: float               # did the system find what a person would make
    precision: float
    false_positive_rate: float  # did it promote what a person would bin
    matched: int
    system_count: int
    human_count: int

    @property
    def f1(self) -> float:
        if self.recall + self.precision == 0:
            return 0.0
        return 2 * self.recall * self.precision / (self.recall + self.precision)


def score_content_discovery(
    system_spans: Sequence[Span],
    human_spans: Sequence[Span],
    *,
    min_iou: float | None = None,
    profile: "CalibrationProfile | None" = None,
) -> ContentDiscoveryScore:
    """Match discovered contents to human-marked ones by span overlap.

    How much overlap counts as agreement is a 판정 기준, so 17.1 puts it in the
    profile - ``calibration.content_match_min_iou``. It is loose because
    agreeing that something is content matters more here than agreeing on its
    exact boundaries, which the planner sets later anyway; how loose is a thing
    17.4 measures rather than a thing decided here.

    ``min_iou`` overrides the profile for one call, which is what the sweep
    needs when it is the parameter being swept.
    """
    if min_iou is None:
        min_iou = (
            profile.get_float("calibration.content_match_min_iou")
            if profile is not None else DEFAULT_MIN_IOU
        )
    unmatched = list(human_spans)
    matched = 0
    for span in system_spans:
        best, best_score = None, 0.0
        for candidate in unmatched:
            score = iou(span, candidate)
            if score > best_score:
                best, best_score = candidate, score
        if best is not None and best_score >= min_iou:
            unmatched.remove(best)
            matched += 1

    system_count, human_count = len(system_spans), len(human_spans)
    recall = matched / human_count if human_count else (1.0 if not system_count else 0.0)
    precision = matched / system_count if system_count else 1.0
    return ContentDiscoveryScore(
        recall=round(recall, 4),
        precision=round(precision, 4),
        false_positive_rate=round((system_count - matched) / system_count, 4) if system_count else 0.0,
        matched=matched,
        system_count=system_count,
        human_count=human_count,
    )


def combined_score(
    pacing: PacingScore | None,
    discovery: ContentDiscoveryScore | None,
    *,
    profile: "CalibrationProfile | None" = None,
) -> float:
    """One number for the sweep to maximise.

    Discovery is weighted above pacing because MVP 3 is the project's first gate:
    a system that breathes beautifully around the wrong content is worth less
    than one that finds the right content and paces it adequately. How much
    above is `calibration.score_weights` in the profile (17.1) - the ratio
    decides which profile a sweep picks, so it is not a constant in here.
    """
    weights = (
        profile.get("calibration.score_weights") if profile is not None else DEFAULT_SCORE_WEIGHTS
    )
    parts: list[tuple[float, float]] = []
    if discovery is not None:
        parts.append((float(weights["discovery"]), discovery.f1))
    if pacing is not None:
        parts.append((float(weights["pacing"]), pacing.f1))
    if not parts:
        return 0.0
    total_weight = sum(w for w, _ in parts)
    return round(sum(w * v for w, v in parts) / total_weight, 4)
