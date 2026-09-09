"""Source time <-> output time.

An episode's cuts come from anywhere in the source and in any order (2.4), and
pacing removes spans from inside them (9.3). So "when does this line appear in
the finished video" is a real computation, and both the subtitle writer and the
chapter list depend on getting it right.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from aicut.models import Cut


@dataclass
class Segment:
    """One continuous piece of source that survives into the output."""

    cut_index: int
    sequence_order: int
    source_start_sec: float
    source_end_sec: float
    out_start_sec: float

    @property
    def duration(self) -> float:
        return max(0.0, self.source_end_sec - self.source_start_sec)

    @property
    def out_end_sec(self) -> float:
        return self.out_start_sec + self.duration


class Timeline:
    """The ordered segment list of a finished episode."""

    def __init__(self, segments: list[Segment]):
        self.segments = segments

    @classmethod
    def from_cuts(cls, cuts: Sequence[Cut]) -> "Timeline":
        segments: list[Segment] = []
        clock = 0.0
        for index, cut in enumerate(sorted(cuts, key=lambda c: c.sequence_order)):
            for start, end in cut.kept_spans():
                segment = Segment(
                    cut_index=index,
                    sequence_order=cut.sequence_order,
                    source_start_sec=start,
                    source_end_sec=end,
                    out_start_sec=clock,
                )
                segments.append(segment)
                clock += segment.duration
        return cls(segments)

    @property
    def duration(self) -> float:
        return self.segments[-1].out_end_sec if self.segments else 0.0

    def to_output(self, source_sec: float, *, sequence_order: int | None = None) -> float | None:
        """Where a source moment lands in the output.

        A source second can appear more than once - repeating a scene is a
        legitimate editing choice (4.3) - so pass ``sequence_order`` to say which
        occurrence you mean. Returns None when that moment was cut.
        """
        for segment in self.segments:
            if sequence_order is not None and segment.sequence_order != sequence_order:
                continue
            if segment.source_start_sec <= source_sec <= segment.source_end_sec:
                return segment.out_start_sec + (source_sec - segment.source_start_sec)
        return None

    def cut_starts(self) -> dict[int, float]:
        """Where each cut begins in the finished video, by sequence order.

        Chapter marks (11.2) and sequence markers are placed from this.

        A cut pacing removed entirely has no segment and so no start. Reading
        the starts as a bare list and pairing it with the cuts in order put
        every later cut's chapter mark at the previous cut's time.
        """
        out: dict[int, float] = {}
        for segment in self.segments:
            out.setdefault(segment.sequence_order, segment.out_start_sec)
        return out
