"""Loop B: learning from source-and-finished-video pairs (12.3 B, MVP 4).

This is the loop that separates a producer from a rule engine. Loops A and C say
what tends to work on the platform and what worked afterwards; only B shows the
actual editing decision - out of six hours, *this* is what a person kept, this is
what they dropped, this is the order they put it in, this is what they repeated
and what they emphasised.

The same alignment doubles as the calibration dataset of 17.2, so this module
also produces the labelled span pairs the sweep scores against - one piece of
work serving both purposes, as 17.2 notes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from aicut.db.store import Store
from aicut.llm import Producer
from aicut.models import Utterance

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Interval:
    """A stretch of the source, used for the set arithmetic below."""

    start_sec: float
    end_sec: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


def merge_intervals(intervals: Sequence[Interval]) -> list[Interval]:
    """Overlapping and touching stretches folded into one."""
    merged: list[Interval] = []
    for interval in sorted(intervals, key=lambda item: (item.start_sec, item.end_sec)):
        if not merged or interval.start_sec > merged[-1].end_sec:
            merged.append(interval)
        else:
            previous = merged[-1]
            merged[-1] = Interval(previous.start_sec, max(previous.end_sec, interval.end_sec))
    return merged


def complement(intervals: Sequence[Interval], duration_sec: float) -> list[Interval]:
    """What is left of the source once the given stretches are taken out.

    This is the half of 12.3 B that the transcript alone cannot give. Speech
    alignment only ever produces spans where somebody was talking, so a keep
    ratio computed from those spans measures "of the talking, how much
    survived" — while 12.3 B asks what the editor removed from the broadcast,
    and most of what an editor removes is the farming, the walking and the
    away-from-desk, where nobody says anything at all.

    Taken from the Codex build's `learning.py`, which had this and the ratio
    below right; the alignment that feeds it is this module's.
    """
    cursor = 0.0
    removed: list[Interval] = []
    for interval in merge_intervals(intervals):
        if interval.start_sec > cursor:
            removed.append(Interval(cursor, interval.start_sec))
        cursor = max(cursor, interval.end_sec)
    if cursor < duration_sec:
        removed.append(Interval(cursor, duration_sec))
    return removed


@dataclass
class AlignedSpan:
    """A source span and where it ended up in the human's finished video."""

    source_start_sec: float
    source_end_sec: float
    output_start_sec: float | None = None
    output_end_sec: float | None = None
    kept: bool = True
    order_changed: bool = False
    repeated: int = 1
    text: str = ""

    @property
    def source_duration(self) -> float:
        return max(0.0, self.source_end_sec - self.source_start_sec)

    @property
    def compression(self) -> float:
        """How much shorter the span got. 1.0 means untouched, 0 means cut."""
        if not self.kept or self.output_end_sec is None or self.output_start_sec is None:
            return 0.0
        if self.source_duration <= 0:
            return 0.0
        return (self.output_end_sec - self.output_start_sec) / self.source_duration

    # 강조 was here, decided by code from `compression` and `repeated` against a
    # margin written into the source. 18장 gives 편집 의도 to the AI, and 12.3 B
    # asks it what was 선택/제거/재배치/반복/강조 - so the measurements go into
    # the payload and the answer comes back from the analysis, not from here.


@dataclass
class Alignment:
    """The full mapping between one source and one human-made output."""

    source_ref: str
    output_ref: str
    spans: list[AlignedSpan] = field(default_factory=list)
    #: Length of the source broadcast. Zero means it was not supplied, and the
    #: whole-timeline figures below are then unavailable rather than guessed.
    source_duration_sec: float = 0.0

    @property
    def kept_spans(self) -> list[AlignedSpan]:
        return [s for s in self.spans if s.kept]

    @property
    def keep_ratio(self) -> float:
        """Of the speech, how much survived. Not the same as `selection_ratio`."""
        total = sum(s.source_duration for s in self.spans)
        kept = sum(s.source_duration for s in self.kept_spans)
        return kept / total if total else 0.0

    def selected_segments(self) -> list[Interval]:
        """The stretches of source the editor used, overlaps folded together."""
        return merge_intervals([
            Interval(s.source_start_sec, s.source_end_sec) for s in self.kept_spans
        ])

    def removed_segments(self) -> list[Interval]:
        """What the editor threw away, across the whole broadcast.

        Includes every stretch with no speech in it, which is where most of a
        six-hour broadcast goes and which the span list cannot see.
        """
        if self.source_duration_sec <= 0:
            return []
        return complement(self.selected_segments(), self.source_duration_sec)

    @property
    def selection_ratio(self) -> float:
        """Of the whole broadcast, how much reached the finished video."""
        if self.source_duration_sec <= 0:
            return 0.0
        used = sum(item.duration for item in self.selected_segments())
        return used / self.source_duration_sec

    def reordered(self) -> bool:
        outputs = [s.output_start_sec for s in self.kept_spans if s.output_start_sec is not None]
        sources = [s.source_start_sec for s in self.kept_spans if s.output_start_sec is not None]
        ranked = [x for _, x in sorted(zip(outputs, sources))]
        return ranked != sorted(ranked)


def align_by_transcript(
    source_utterances: Sequence[Utterance],
    output_utterances: Sequence[Utterance],
    *,
    min_overlap: float = 0.6,
    source_duration_sec: float = 0.0,
) -> Alignment:
    """Match the finished video's speech back to the source's speech.

    Text is the anchor because the output has been cut, sped up, reordered and
    overlaid, but the words are still the words. Anything in the source that no
    output line matches is what the editor threw away - which is the more
    informative half of the signal.
    """
    def norm(text: str) -> list[str]:
        return [w.lower().strip(".,!?\"'") for w in text.split() if w.strip(".,!?\"'")]

    output_tokens = [(u, set(norm(u.text))) for u in output_utterances]
    spans: list[AlignedSpan] = []

    for source in source_utterances:
        tokens = set(norm(source.text))
        # Every output line this source line reaches, not just the best one.
        # `repeated` is "how many times the editor used this moment" (12.3 B),
        # so it has to count the output occurrences of one source span. Keeping
        # a single best match and then counting how many *source* spans landed
        # on it measured the opposite: a moment genuinely used twice stayed at
        # 1, while two similar source lines colliding on one output line were
        # both labelled a repeat that never happened.
        matches = []
        for utterance, other in output_tokens:
            if not other or not tokens:
                continue
            overlap = len(tokens & other) / max(1, min(len(tokens), len(other)))
            if overlap >= min_overlap:
                matches.append((overlap, utterance))

        if matches:
            # The strongest match represents the span; reordering is measured
            # from where the editor put it, and that is the place it best fits.
            best = max(matches, key=lambda m: m[0])[1]
            spans.append(AlignedSpan(
                source_start_sec=source.start_sec,
                source_end_sec=source.end_sec,
                output_start_sec=best.start_sec,
                output_end_sec=best.end_sec,
                kept=True,
                repeated=len(matches),
                text=source.text,
            ))
        else:
            spans.append(AlignedSpan(
                source_start_sec=source.start_sec,
                source_end_sec=source.end_sec,
                kept=False,
                text=source.text,
            ))

    # Per span, not one verdict copied onto all of them. This used to assign
    # `alignment.reordered()` to every kept span, so a video with one backwards
    # jump reported that every moment had moved — which is the whole-video
    # question, already answered by `reordered()`, and it buries the decision
    # 12.3 B is trying to learn.
    _mark_order_changes(spans)
    return Alignment(
        source_ref="", output_ref="", spans=spans, source_duration_sec=source_duration_sec,
    )


def _mark_order_changes(spans: Sequence[AlignedSpan]) -> None:
    """Mark the spans the editor moved backwards in time (2.4).

    `Alignment.reordered()` answers whether the edit is non-linear at all. 12.3 B
    asks how the order was changed, which means knowing *which* moments jumped —
    the 05:12 that the editor put before the 01:42 is the decision being learnt,
    and a single boolean for the whole video does not carry it.
    """
    placed = sorted(
        (s for s in spans if s.kept and s.output_start_sec is not None),
        key=lambda s: s.output_start_sec,
    )
    furthest = None
    for span in placed:
        if furthest is not None and span.source_start_sec < furthest:
            span.order_changed = True
        furthest = span.source_start_sec if furthest is None else max(furthest, span.source_start_sec)


def learn(
    producer: Producer,
    store: Store,
    alignment: Alignment,
    *,
    source_ref: str,
    output_ref: str,
    context: dict[str, Any] | None = None,
    source_frames: Sequence[str] = (),
    output_frames: Sequence[str] = (),
) -> dict[str, Any]:
    """Turn one alignment into stated editing rules and store the pair.

    ``source_frames`` and ``output_frames`` are frames sampled across the two
    videos. 5.2 says the passes do not separate 화면 from 소리, and 12.3 B asks
    what was 선택/제거/재배치/반복/강조 - 강조 in particular is a caption, a zoom
    or an effect, none of which reach a transcript. The alignment below is one
    signal in the payload; the answer comes from the analysis looking at both.
    """
    payload = {
        "source_ref": source_ref,
        "output_ref": output_ref,
        "keep_ratio": round(alignment.keep_ratio, 4),
        "reordered": alignment.reordered(),
        "kept": [
            {
                "source": [s.source_start_sec, s.source_end_sec],
                "output": [s.output_start_sec, s.output_end_sec],
                "compression": round(s.compression, 3),
                "repeated": s.repeated,
                "order_changed": s.order_changed,
                "text": s.text[:200],
            }
            for s in alignment.kept_spans
        ],
        "dropped": [
            {"source": [s.source_start_sec, s.source_end_sec], "text": s.text[:200]}
            for s in alignment.spans if not s.kept
        ],
        # 12.3 B asks what was removed, and most of what an editor removes has
        # no speech in it. These are the whole-broadcast figures; the span lists
        # above only ever cover the talking.
        "source_duration_sec": alignment.source_duration_sec,
        "selection_ratio": round(alignment.selection_ratio, 4),
        "selected_segments": [
            [round(i.start_sec, 2), round(i.end_sec, 2)] for i in alignment.selected_segments()
        ],
        "removed_segments": [
            [round(i.start_sec, 2), round(i.end_sec, 2)] for i in alignment.removed_segments()
        ],
        "context": context or {},
    }
    frames = list(source_frames) + list(output_frames)
    if frames:
        payload["frames"] = {
            "source": len(source_frames),
            "output": len(output_frames),
            "order": "the source frames come first, in time order, then the output frames",
        }
    analysis = producer.compare_source_output(payload, images=frames)
    analysis["measured"] = {
        "keep_ratio": payload["keep_ratio"],
        "reordered": payload["reordered"],
        "kept_spans": len(alignment.kept_spans),
        "dropped_spans": len(alignment.spans) - len(alignment.kept_spans),
        "moved_spans": sum(1 for s in alignment.kept_spans if s.order_changed),
        "source_duration_sec": alignment.source_duration_sec,
        "selection_ratio": payload["selection_ratio"],
        "removed_segments": len(payload["removed_segments"]),
        "removed_sec": round(
            sum(i.duration for i in alignment.removed_segments()), 2,
        ),
    }
    store.save_source_output_pair(source_ref, output_ref, analysis)
    return analysis
