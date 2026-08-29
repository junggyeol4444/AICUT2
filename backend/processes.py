from __future__ import annotations

import subprocess
import threading
from collections.abc import Sequence


class ProcessSupervisor:
    """Tracks child processes by project so cancellation reaches external tools immediately."""

    def __init__(self) -> None:
        self._processes: dict[str, set[subprocess.Popen]] = {}
        self._lock = threading.Lock()

    def runner(self, project_id: str, cancelled: threading.Event):
        def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
            if cancelled.is_set():
                return subprocess.CompletedProcess(command, 130, "", "cancelled")
            timeout = kwargs.pop("timeout", None)
            check = kwargs.pop("check", False)
            capture = kwargs.pop("capture_output", False)
            text = kwargs.pop("text", False)
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                text=text,
                **kwargs,
            )
            with self._lock:
                self._processes.setdefault(project_id, set()).add(process)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                raise
            finally:
                with self._lock:
                    self._processes.get(project_id, set()).discard(process)
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if check and result.returncode:
                raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
            return result
        return run

    def cancel(self, project_id: str) -> int:
        with self._lock:
            processes = list(self._processes.get(project_id, ()))
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
        return len(processes)

    def pids(self, project_id: str) -> list[int]:
        with self._lock:
            return sorted(process.pid for process in self._processes.get(project_id, ()) if process.poll() is None)
