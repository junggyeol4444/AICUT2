import threading
import unittest

from backend.scheduler import RuntimeScheduler


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


if __name__ == "__main__":
    unittest.main()
