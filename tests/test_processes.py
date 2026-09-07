import sys
import threading
import time
import unittest

from backend.processes import ProcessSupervisor


class ProcessSupervisorTest(unittest.TestCase):
    def test_cancel_terminates_the_running_child_process(self):
        supervisor = ProcessSupervisor()
        cancelled = threading.Event()
        result = {}

        def execute():
            result["value"] = supervisor.runner("project-1", cancelled)(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True, text=True, check=False,
            )

        worker = threading.Thread(target=execute)
        worker.start()
        deadline = time.monotonic() + 2
        while supervisor.cancel("unknown") == 0 and time.monotonic() < deadline:
            with supervisor._lock:
                if supervisor._processes.get("project-1"):
                    break
            time.sleep(0.01)
        self.assertEqual(supervisor.cancel("project-1"), 1)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertNotEqual(result["value"].returncode, 0)

    def test_runner_preserves_completed_process_output(self):
        result = ProcessSupervisor().runner("project-2", threading.Event())(
            [sys.executable, "-c", "print('ready')"], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ready")


if __name__ == "__main__":
    unittest.main()
