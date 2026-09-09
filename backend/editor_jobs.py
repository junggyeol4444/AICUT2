from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def write_job_state(path: str | Path, state: Mapping[str, Any]) -> None:
    """Atomically publish editor-job state for polling plugin UIs."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(state), stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def launch_editor_job(
    aicut_root: str | Path, media_path: str, workspace: str | Path, *, fps: int = 30,
    python_executable: str = "python3", options_json: str | None = None,
    manifest_path: str | None = None,
) -> dict[str, Any]:
    """Launch analysis outside an editor host and return its pollable job state."""
    root = Path(aicut_root).expanduser().resolve()
    source = Path(media_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"선택한 편집기 미디어를 찾을 수 없습니다: {source}")
    bridge = root / "editor_plugins" / "run_bridge.py"
    if not bridge.is_file():
        raise ValueError(f"AICUT bridge 실행 파일을 찾을 수 없습니다: {bridge}")
    job_id = str(uuid.uuid4())
    job_root = Path(workspace).expanduser().resolve() / "jobs"
    job_root.mkdir(parents=True, exist_ok=True)
    state_path = job_root / f"{job_id}.json"
    log_path = job_root / f"{job_id}.log"
    command = [
        python_executable, str(bridge), "--media", str(source), "--workspace", str(Path(workspace).expanduser()),
        "--fps", str(int(fps)), "--result-file", str(state_path), "--job-id", job_id,
    ]
    if options_json:
        command.extend(("--options-json", str(Path(options_json).expanduser())))
    if manifest_path:
        command.extend(("--manifest", str(Path(manifest_path).expanduser())))
    write_job_state(state_path, {
        "job_id": job_id, "status": "QUEUED", "media_path": str(source),
        "created_at": datetime.now(timezone.utc).isoformat(), "log_path": str(log_path),
    })
    with log_path.open("ab") as log:
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
    return {
        "job_id": job_id, "pid": process.pid, "status": "QUEUED",
        "state_path": str(state_path), "log_path": str(log_path), "command": command,
    }
