"""The MVP 2 실측 항목: 1차 통과 밀도별 사건 검출률과 처리 시간 (19장).

19장 gives MVP 2 a success criterion and a 실측 항목, and only the second is
something to build:

    - 성공 기준: 사람이 기억하는 주요 사건을 누락 없이 잡아내는가
    - 실측 항목: 1차 통과 밀도별 사건 검출률과 처리 시간

The 성공 기준 is a standard, not a task. Whether the run caught what a person
would remember is answered by that person reading the result - the clause names
no list to write, and MVP 2's 입력 is already stated as 4~6시간 생방송 1편.

The 실측 항목 is measurable and this measures it. The density is
``scan.pass1_window_sec``: a shorter window is a denser pass, which sees more
and costs more. 19장 asks for the curve, not for a value to be chosen.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from aicut.errors import AicutError

log = logging.getLogger(__name__)


@dataclass
class DensityMeasurement:
    """One point on the curve 19장 asks for."""

    pass1_window_sec: float
    events: int
    seconds: float
    realtime_factor: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pass1_window_sec": self.pass1_window_sec,
            "events": self.events,
            "seconds": round(self.seconds, 2),
            "realtime_factor": (
                round(self.realtime_factor, 1) if self.realtime_factor is not None else None
            ),
        }


def measure_densities(
    ctx: Any,
    densities: Sequence[float],
    *,
    understand: Callable[[Any], Any],
) -> list[DensityMeasurement]:
    """Run the understanding pass once per density and time it.

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
            # perf_counter, not time(): the wall clock can step, and on Windows
            # it ticks about every 15ms - a fast pass measured 0.0s there and
            # the realtime factor came back undefined.
            started = time.perf_counter()
            understand(ctx)
            elapsed = time.perf_counter() - started
            events = scratch.events(ctx.project.project_id)
            log.info("density %ss: %d events in %.1fs", window_sec, len(events), elapsed)
            rows.append(DensityMeasurement(
                pass1_window_sec=float(window_sec),
                events=len(events),
                seconds=elapsed,
                realtime_factor=(duration / elapsed) if elapsed > 0 and duration else None,
            ))
    finally:
        ctx.profile, ctx.store = base_profile, base_store
        scratch.close()
    return rows
