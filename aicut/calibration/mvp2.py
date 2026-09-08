"""The MVP 2 gate: 1차 통과 밀도별 사건 검출률과 처리 시간 (19장).

19장 gives MVP 2 one success criterion and one 실측 항목, and they are different
questions:

* 성공 기준 - 사람이 기억하는 주요 사건을 누락 없이 잡아내는가. Only a person who
  watched the broadcast knows what those events are, so they write them down and
  this measures whether a detected event covers each one.
* 실측 항목 - 1차 통과 밀도별 사건 검출률과 처리 시간. The density is
  ``scan.pass1_window_sec``: a shorter window is a denser pass, which sees more
  and costs more. 19장 asks for the curve, not a chosen value.

Nothing here decides whether the gate passed. Coverage is arithmetic on time
spans - a detected event either overlaps the moment a person wrote down or it
does not - and how much coverage is enough is the person's call, as 19장 has it.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from aicut.errors import AicutError

log = logging.getLogger(__name__)

#: How far from the moment a person wrote down a detected event may sit and
#: still count as the same event. A person recalling a six-hour broadcast writes
#: "about 40 minutes in", not a frame number, and a first pass whose windows are
#: minutes long cannot be scored against a stopwatch. Widen it with
#: ``--tolerance`` when the recollection is rougher than this.
DEFAULT_TOLERANCE_SEC = 90.0


@dataclass
class RememberedEvent:
    """One thing a person remembers happening, and roughly when.

    ``end_sec`` is optional: some events are a moment, some are a stretch. When
    it is absent the event is the moment at ``at_sec``.
    """

    at_sec: float
    what: str
    end_sec: float | None = None

    def span(self, tolerance_sec: float) -> tuple[float, float]:
        end = self.end_sec if self.end_sec is not None else self.at_sec
        return (self.at_sec - tolerance_sec, end + tolerance_sec)


def load_remembered(path: str | Path) -> list[RememberedEvent]:
    """Read the person's list of 주요 사건.

    The file is a JSON list of ``{"at_sec": .., "what": ".."}``; ``end_sec`` is
    optional. Written by hand, so the errors say what the file should look like.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("events", raw.get("remembered", []))
    if not isinstance(raw, list) or not raw:
        raise AicutError(
            "a remembered-events file is a non-empty JSON list of"
            ' {"at_sec": 1234, "what": "무슨 일이 있었는지"}'
        )
    events: list[RememberedEvent] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or "at_sec" not in item:
            raise AicutError(f"remembered event {index} has no 'at_sec': {item!r}")
        end = item.get("end_sec")
        events.append(RememberedEvent(
            at_sec=float(item["at_sec"]),
            what=str(item.get("what", "")),
            end_sec=float(end) if end is not None else None,
        ))
    return sorted(events, key=lambda e: e.at_sec)


@dataclass
class Coverage:
    """Which remembered events the run found, and which it missed."""

    found: list[dict[str, Any]] = field(default_factory=list)
    missed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rate(self) -> float | None:
        total = len(self.found) + len(self.missed)
        return round(len(self.found) / total, 3) if total else None


def coverage(
    events: Sequence[Any],
    remembered: Sequence[RememberedEvent],
    *,
    tolerance_sec: float = DEFAULT_TOLERANCE_SEC,
) -> Coverage:
    """Match detected events to remembered ones by time, and nothing else.

    Overlap is the only test. Deciding that a detected event *is* the one a
    person remembers is a judgement about meaning, and 18장 does not put that
    here - the report names the detected event so the person can check it.
    """
    result = Coverage()
    for want in remembered:
        low, high = want.span(tolerance_sec)
        hits = []
        for event in events:
            start, end = event.span()
            if end >= low and start <= high:
                hits.append(event)
        row = {
            "at_sec": want.at_sec,
            "end_sec": want.end_sec,
            "what": want.what,
            "events": [
                {"event_id": e.event_id, "summary": e.summary, "span": list(e.span())}
                for e in hits
            ],
        }
        (result.found if hits else result.missed).append(row)
    return result


@dataclass
class DensityMeasurement:
    """One point on the curve 19장 asks for."""

    pass1_window_sec: float
    events: int
    seconds: float
    realtime_factor: float | None
    coverage: Coverage | None = None
    duration_sec: float = 0.0

    def widest_match_ratio(self) -> float | None:
        """How much of the broadcast the largest matching event covers.

        A single event spanning the whole source matches every remembered
        moment, and would report a perfect detection rate while having detected
        nothing. The rate alone cannot show that; this can.
        """
        if self.coverage is None or not self.duration_sec:
            return None
        spans = [
            event["span"] for row in self.coverage.found for event in row["events"]
        ]
        if not spans:
            return None
        return round(max(end - start for start, end in spans) / self.duration_sec, 3)

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "pass1_window_sec": self.pass1_window_sec,
            "events": self.events,
            "seconds": round(self.seconds, 2),
            "realtime_factor": (
                round(self.realtime_factor, 1) if self.realtime_factor is not None else None
            ),
        }
        if self.coverage is not None:
            row["detection_rate"] = self.coverage.rate
            row["found"] = len(self.coverage.found)
            row["missed"] = [m["what"] or f"@{m['at_sec']:.0f}s" for m in self.coverage.missed]
            row["widest_match_ratio"] = self.widest_match_ratio()
        return row


def measure_densities(
    ctx: Any,
    densities: Sequence[float],
    *,
    understand: Callable[[Any], Any],
    remembered: Sequence[RememberedEvent] = (),
    tolerance_sec: float = DEFAULT_TOLERANCE_SEC,
) -> list[DensityMeasurement]:
    """Run the understanding pass once per density and measure both numbers.

    The runs happen against a scratch copy of the workspace database, not the
    project's own. ``understanding.run`` is a production stage - it calls
    ``replace_windows``, ``replace_details`` and ``replace_events`` - so running
    it here on the live store would throw away the project's understanding and
    leave its candidates pointing at event ids that no longer exist. A
    measurement must not cost the thing it measures.

    One copy serves every density: each run overwrites its own scratch rows,
    while the utterances and situations it reads stay as they were.
    """
    from aicut.db.store import Store

    if not densities:
        raise AicutError("measuring the density curve needs at least one density")
    base_profile, base_store = ctx.profile, ctx.store
    duration = getattr(ctx.project, "duration_sec", 0.0) or 0.0
    scratch = Store(":memory:")
    base_store.conn.backup(scratch.conn)
    rows: list[DensityMeasurement] = []
    try:
        ctx.store = scratch
        for window_sec in densities:
            ctx.profile = base_profile.with_overrides({"scan.pass1_window_sec": window_sec})
            started = time.time()
            understand(ctx)
            elapsed = time.time() - started
            events = scratch.events(ctx.project.project_id)
            log.info("density %ss: %d events in %.1fs", window_sec, len(events), elapsed)
            rows.append(DensityMeasurement(
                pass1_window_sec=float(window_sec),
                events=len(events),
                seconds=elapsed,
                realtime_factor=(duration / elapsed) if elapsed > 0 and duration else None,
                coverage=(
                    coverage(events, remembered, tolerance_sec=tolerance_sec)
                    if remembered else None
                ),
                duration_sec=duration,
            ))
    finally:
        ctx.profile, ctx.store = base_profile, base_store
        scratch.close()
    return rows
