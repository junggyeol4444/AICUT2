import threading
import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend.scheduler import PeriodicTask, RuntimeScheduler


class RuntimeSchedulerTest(unittest.TestCase):
    def test_task_failures_are_isolated_and_reported(self):
        calls = []

        def success():
            calls.append("success")
            return {"submitted": 2}

        def failure():
            raise RuntimeError("temporary failure")

        scheduler = RuntimeScheduler({"uploads": success, "analytics": failure}, 1)
        result = scheduler.run_once()
        self.assertEqual(calls, ["success"])
        self.assertEqual(result["uploads"]["result"], {"submitted": 2})
        self.assertEqual(result["analytics"], {"status": "FAILED", "error": "temporary failure"})
        self.assertIsNotNone(scheduler.status()["last_run_at"])

    def test_background_loop_starts_once_and_stops_cooperatively(self):
        called = threading.Event()
        scheduler = RuntimeScheduler({"queue": called.set}, .01)
        self.assertTrue(scheduler.start())
        self.assertFalse(scheduler.start())
        self.assertTrue(called.wait(1))
        scheduler.stop(1)
        self.assertFalse(scheduler.status()["running"])

    def test_interval_must_be_positive(self):
        with self.assertRaises(ValueError):
            RuntimeScheduler({}, 0)

    def test_periodic_task_runs_only_after_its_independent_interval(self):
        now = [100.0]
        calls = []
        task = PeriodicTask(lambda: calls.append("backup") or "created", 60, clock=lambda: now[0])
        self.assertEqual(task()["status"], "SKIPPED")
        now[0] = 159.0
        self.assertEqual(task()["status"], "SKIPPED")
        now[0] = 160.0
        self.assertEqual(task(), "created")
        self.assertEqual(calls, ["backup"])

    def test_periodic_task_can_run_immediately(self):
        task = PeriodicTask(lambda: "ran", 60, run_immediately=True, clock=lambda: 100.0)
        self.assertEqual(task(), "ran")

    def test_scheduler_runs_are_persisted_for_restart_observability(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "scheduler.db")
            scheduler = RuntimeScheduler(
                {"uploads": lambda: {"submitted": ["one"]}}, 60,
                on_run=lambda results, completed_at: database.save_scheduler_run(results, completed_at),
            )
            scheduler.run_once()
            runs = database.list_scheduler_runs()
            with self.assertRaises(ValueError):
                database.list_scheduler_runs(0)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["results"]["uploads"]["status"], "COMPLETE")
        self.assertEqual(runs[0]["results"]["uploads"]["result"]["submitted"], ["one"])

    def test_scheduler_continues_when_run_persistence_fails(self):
        scheduler = RuntimeScheduler(
            {"task": lambda: "done"}, 60,
            on_run=lambda _results, _completed: (_ for _ in ()).throw(RuntimeError("db unavailable")),
        )
        self.assertEqual(scheduler.run_once()["task"]["result"], "done")
        self.assertEqual(scheduler.status()["results"]["persistence"]["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
