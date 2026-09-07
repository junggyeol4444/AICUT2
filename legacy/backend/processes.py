from __future__ import annotations

import os
import signal
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
            popen_options = dict(kwargs)
            if os.name == "posix":
                popen_options.setdefault("start_new_session", True)
            elif os.name == "nt":
                popen_options.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                text=text,
                **popen_options,
            )
            with self._lock:
                self._processes.setdefault(project_id, set()).add(process)
            if cancelled.is_set():
                self._signal(process, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._signal(process, signal.SIGKILL)
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
                self._signal(process, signal.SIGTERM)
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._signal(process, signal.SIGKILL)
        return len(processes)

    def pids(self, project_id: str) -> list[int]:
        with self._lock:
            return sorted(process.pid for process in self._processes.get(project_id, ()) if process.poll() is None)

    @staticmethod
    def _signal(process: subprocess.Popen, sig: signal.Signals) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, sig)
                return
            except ProcessLookupError:
                return
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
