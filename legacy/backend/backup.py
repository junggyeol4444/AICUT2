from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .database import Database


class DatabaseBackupManager:
    """Creates atomic SQLite snapshots and prunes only AICUT-owned backup files."""

    def __init__(
        self, database: Database, directory: str | Path, *, retention_count: int = 7,
        clock: Callable[[], datetime] | None = None,
    ):
        if retention_count <= 0:
            raise ValueError("backup retention_count는 0보다 커야 합니다.")
        self.database = database
        self.directory = Path(directory).expanduser().resolve()
        self.retention_count = retention_count
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def create(self) -> dict:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("SQLite backup이 이미 실행 중입니다.")
        try:
            moment = self.clock()
            if moment.tzinfo is None:
                raise ValueError("backup clock에는 timezone 정보가 필요합니다.")
            stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            destination = self.directory / f"aicut-{stamp}.sqlite3"
            result = self.database.backup_to(destination)
            backups = sorted(self.directory.glob("aicut-*.sqlite3"), reverse=True)
            removed = []
            for stale in backups[self.retention_count:]:
                stale.unlink()
                removed.append(str(stale))
            return {**result, "removed": removed}
        finally:
            self._lock.release()

    def list(self) -> list[dict]:
        if not self.directory.exists():
            return []
        backups = []
        for path in sorted(self.directory.glob("aicut-*.sqlite3"), reverse=True):
            stat = path.stat()
            backups.append({
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
        return backups
