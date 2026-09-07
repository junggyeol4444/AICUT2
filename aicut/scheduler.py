"""In-process scheduler for the queues that must drain without an operator.

Two of the plan's requirements outlive a single command. 11.4 puts a failed
upload in a retry queue aimed at the next PT midnight — but a queue nothing
drains is just a list. And a snapshot policy the operator has to remember to
run is not a policy. Both need something ticking while `aicut ui` is up.

Ported from the Codex build's `legacy/backend/scheduler.py`. Two ideas are
theirs and both are right:

- **Failure isolation.** One task raising must not stop the others. A quota
  error draining uploads is routine; it cannot be allowed to skip the backup.
- **A separate monotonic gate per task** (:class:`Periodic`). The tick interval
  is set by the fastest queue, but a daily backup must not fire every minute
  just because the loop does. Monotonic, so a clock adjustment cannot make a
  daily job fire hourly or stall for a day.

Changed in the port: a task that overruns the interval no longer stacks. The
original held `_run_lock` non-blocking inside `run_once`, which is right, but
its loop then still waited the full interval afterwards — so a tick that took
longer than the interval silently halved the effective rate. Here the loop
subtracts the elapsed time, and reports drift instead of hiding it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

Task = Callable[[], Any]


class Periodic:
    """Wraps a task so it runs at most once per `interval_sec`, monotonic."""

    def __init__(
        self,
        task: Task,
        interval_sec: float,
        *,
        run_immediately: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        if interval_sec <= 0:
            raise ValueError(f"a periodic interval must be positive, got {interval_sec}")
        self.task = task
        self.interval_sec = float(interval_sec)
        self.clock = clock
        self._lock = threading.Lock()
        self._due_at = self.clock() if run_immediately else self.clock() + self.interval_sec

    def __call__(self) -> Any:
        now = self.clock()
        with self._lock:
            if now < self._due_at:
                return {"status": "SKIPPED", "due_in_sec": round(self._due_at - now, 3)}
            self._due_at = now + self.interval_sec
        return self.task()


class Scheduler:
    """Runs a named set of tasks on a loop. Task failures are isolated."""

    def __init__(
        self,
        tasks: dict[str, Task],
        interval_sec: float = 60.0,
        *,
        on_run: Callable[[dict[str, Any], str], None] | None = None,
    ):
        if interval_sec <= 0:
            raise ValueError(f"the scheduler interval must be positive, got {interval_sec}")
        self.tasks = dict(tasks)
        self.interval_sec = float(interval_sec)
        self.on_run = on_run
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_run_at: str | None = None
        self._results: dict[str, Any] = {}

    # -- one pass ------------------------------------------------------------
    def run_once(self) -> dict[str, Any]:
        """Run every task once. Never raises: each result records its own outcome."""
        if not self._run_lock.acquire(blocking=False):
            return {"scheduler": {"status": "SKIPPED", "reason": "already running"}}
        try:
            results: dict[str, Any] = {}
            for name, task in self.tasks.items():
                try:
                    results[name] = {"status": "COMPLETE", "result": task()}
                except Exception as exc:  # isolation is the point — see docstring
                    log.warning("scheduled task %s failed: %s", name, exc)
                    results[name] = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
            completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self._state_lock:
                self._last_run_at = completed_at
                self._results = results
            if self.on_run:
                try:
                    self.on_run(results, completed_at)
                except Exception as exc:
                    # Recording history must not cost the work that was done.
                    log.warning("could not record the scheduler run: %s", exc)
                    with self._state_lock:
                        self._results = {
                            **results,
                            "history": {"status": "FAILED", "error": str(exc)},
                        }
            return results
        finally:
            self._run_lock.release()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="aicut-scheduler", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread:
            thread.join(timeout)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "interval_sec": self.interval_sec,
                "last_run_at": self._last_run_at,
                "results": dict(self._results),
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.run_once()
            elapsed = time.monotonic() - started
            if elapsed > self.interval_sec:
                # Say so rather than quietly running at half rate.
                log.warning(
                    "a scheduler tick took %.1fs, longer than the %.1fs interval",
                    elapsed, self.interval_sec,
                )
            self._stop.wait(max(0.0, self.interval_sec - elapsed))
