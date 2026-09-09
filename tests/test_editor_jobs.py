import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.editor_jobs import launch_editor_job, write_job_state


class EditorJobsTest(unittest.TestCase):
    def test_launcher_starts_pollable_job_without_a_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "aicut"
            bridge = root / "editor_plugins" / "run_bridge.py"
            bridge.parent.mkdir(parents=True)
            bridge.write_text("# bridge", encoding="utf-8")
            media = Path(directory) / "live stream.mkv"
            media.write_bytes(b"media")
            workspace = Path(directory) / "workspace"
            with patch("backend.editor_jobs.subprocess.Popen") as popen:
                popen.return_value.pid = 4321
                result = launch_editor_job(root, str(media), workspace, python_executable="python-test", fps=30)
            state = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
            self.assertEqual((result["status"], result["pid"]), ("QUEUED", 4321))
            self.assertEqual((state["job_id"], state["status"]), (result["job_id"], "QUEUED"))
            command, kwargs = popen.call_args
            self.assertEqual(command[0][0:2], ["python-test", str(bridge)])
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_bridge_records_failure_for_plugin_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "job.json"
            write_job_state(state_path, {"job_id": "job-one", "status": "QUEUED", "log_path": "job.log"})
            result = subprocess.run([
                sys.executable, "editor_plugins/run_bridge.py", "--media", str(Path(directory) / "missing.mkv"),
                "--workspace", directory, "--result-file", str(state_path), "--job-id", "job-one",
            ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state["status"], "FAILED")
            self.assertEqual(state["log_path"], "job.log")
            self.assertIn("찾을 수 없습니다", state["error"])
            self.assertIn("started_at", state)
            self.assertIn("completed_at", state)

    def test_atomic_state_writer_replaces_existing_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_job_state(path, {"status": "QUEUED"})
            write_job_state(path, {"status": "COMPLETE", "result": {"project_id": "one"}})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "COMPLETE")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
