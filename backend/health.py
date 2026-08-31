from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from .database import Database


def runtime_readiness(
    database: Database, storage_path: str | Path, scheduler_status: dict,
    *, min_free_bytes: int = 0, required_tools: tuple[str, ...] = (),
    which: Callable[[str], str | None] = shutil.which,
    disk_usage: Callable = shutil.disk_usage,
) -> dict:
    """Return actionable readiness checks without mutating runtime state."""
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes는 음수일 수 없습니다.")
    checks: dict[str, dict] = {}
    try:
        with database.connect() as connection:
            database_result = connection.execute("PRAGMA quick_check").fetchone()[0]
        checks["database"] = {"ok": database_result == "ok", "detail": database_result}
    except Exception as error:
        checks["database"] = {"ok": False, "detail": str(error)}

    storage = Path(storage_path).expanduser().resolve()
    existing = storage if storage.exists() else next(
        (parent for parent in storage.parents if parent.exists()), storage.anchor,
    )
    try:
        free_bytes = disk_usage(existing).free
        checks["storage"] = {
            "ok": free_bytes >= min_free_bytes,
            "free_bytes": free_bytes,
            "minimum_free_bytes": min_free_bytes,
        }
    except Exception as error:
        checks["storage"] = {"ok": False, "detail": str(error)}

    missing = [tool for tool in required_tools if not which(tool)]
    checks["tools"] = {"ok": not missing, "required": list(required_tools), "missing": missing}
    checks["scheduler"] = {
        "ok": bool(scheduler_status.get("running")),
        "last_run_at": scheduler_status.get("last_run_at"),
    }
    ready = all(check["ok"] for check in checks.values())
    return {"status": "READY" if ready else "DEGRADED", "checks": checks}
