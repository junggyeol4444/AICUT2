from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable


class PeriodicTask:
    """Gate a task to its own monotonic interval while a scheduler ticks more frequently."""

    def __init__(
        self, task: Callable[[], object], interval_sec: float, *,
        run_immediately: bool = False, clock: Callable[[], float] = time.monotonic,
    ):
        if interval_sec <= 0:
            raise ValueError("periodic task interval_sec는 0보다 커야 합니다.")
        self.task = task
        self.interval_sec = float(interval_sec)
        self.clock = clock
        now = self.clock()
        self._next_run = now if run_immediately else now + self.interval_sec
        self._lock = threading.Lock()

    def __call__(self) -> object:
        now = self.clock()
        with self._lock:
            if now < self._next_run:
                return {"status": "SKIPPED", "next_run_in_sec": self._next_run - now}
            self._next_run = now + self.interval_sec
        return self.task()


class RuntimeScheduler:
    """Small in-process scheduler for durable queues; task failures are isolated."""

    def __init__(
        self, tasks: dict[str, Callable[[], object]], interval_sec: float = 60,
        *, on_run: Callable[[dict, str], object] | None = None,
    ):
        if interval_sec <= 0:
            raise ValueError("scheduler interval_sec는 0보다 커야 합니다.")
        self.tasks = dict(tasks)
        self.interval_sec = float(interval_sec)
        self.on_run = on_run
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_run_at: str | None = None
        self._results: dict[str, dict] = {}

    def run_once(self) -> dict[str, dict]:
        if not self._run_lock.acquire(blocking=False):
            return {"scheduler": {"status": "SKIPPED", "reason": "already_running"}}
        try:
            results: dict[str, dict] = {}
            for name, task in self.tasks.items():
                try:
                    results[name] = {"status": "COMPLETE", "result": task()}
                except Exception as error:
                    results[name] = {"status": "FAILED", "error": str(error)}
            with self._state_lock:
                self._last_run_at = datetime.now(timezone.utc).isoformat()
                self._results = results
                completed_at = self._last_run_at
            if self.on_run:
                try:
                    self.on_run(results, completed_at)
                except Exception as error:
                    with self._state_lock:
                        self._results = {**results, "persistence": {
                            "status": "FAILED", "error": str(error),
                        }}
            return results
        finally:
            self._run_lock.release()

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="aicut-scheduler", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def status(self) -> dict:
        with self._state_lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "interval_sec": self.interval_sec,
                "last_run_at": self._last_run_at,
                "results": dict(self._results),
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval_sec)
