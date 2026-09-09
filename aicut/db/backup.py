"""Atomic snapshots of the workspace database.

The database is the only place the expensive work survives: the window
summaries and event graph of 5장 (181 inference calls on a six-hour source),
every human verdict from 15.4, and the calibration profiles of 17장. A render
can be re-run from a saved plan (16장); none of that can be re-derived for
free. So it gets snapshots.

Ported from the Codex build's `legacy/backend/backup.py`. The design is theirs:
SQLite's online backup API rather than a file copy, so the snapshot is a
committed, consistent image taken while the pipeline keeps writing; a
`PRAGMA quick_check` before the file is published; a SHA-256 the operator can
compare later; and pruning restricted to files this module itself named, so a
retention sweep can never take something else out of the directory.

Changed in the port: every temporary connection is closed explicitly. The
original wrote `with sqlite3.connect(tmp) as conn:` — on a sqlite3 connection
that block is a *transaction* manager, not a closing one, so both helper
connections stayed open. On Linux that is a handle leak; on Windows it holds a
lock and the `os.replace` that publishes the snapshot fails outright. This
project's CI has already been bitten once by exactly that shape (a UI thread
opening connections it never closed, invisible on Linux, fatal on Windows), so
it is not a hypothetical.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aicut.errors import AicutError

#: Snapshots this module owns. Pruning matches this and nothing else.
NAME_PREFIX = "aicut-"
NAME_SUFFIX = ".sqlite3"
_READ_CHUNK = 1024 * 1024


class BackupError(AicutError):
    """A snapshot could not be taken, or failed its integrity check."""


class DatabaseBackup:
    """Takes and prunes snapshots of one workspace database."""

    def __init__(
        self,
        db_path: str | Path,
        directory: str | Path,
        *,
        retention: int = 7,
        clock: Callable[[], datetime] | None = None,
    ):
        if retention <= 0:
            raise BackupError(f"backup retention must be at least 1, got {retention}")
        self.db_path = Path(db_path).expanduser().resolve()
        self.directory = Path(directory).expanduser().resolve()
        self.retention = retention
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def create(self) -> dict[str, Any]:
        """Snapshot the database, verify it, publish it, prune older ones."""
        if not self._lock.acquire(blocking=False):
            # Two overlapping snapshots would race on the same temporary name
            # space for no gain; the scheduler's next tick will take one.
            return {"status": "SKIPPED", "reason": "a backup is already running"}
        try:
            if not self.db_path.is_file():
                raise BackupError(f"no database to back up at {self.db_path}")
            moment = self.clock()
            if moment.tzinfo is None:
                raise BackupError("the backup clock must return an aware datetime")
            # Lexicographic order equals chronological order in this format,
            # which is what lets the prune below sort by name alone.
            stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            target = self.directory / f"{NAME_PREFIX}{stamp}{NAME_SUFFIX}"
            if target.resolve() == self.db_path:
                raise BackupError("the backup destination is the database itself")

            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                integrity = self._snapshot(temporary)
                if integrity != "ok":
                    raise BackupError(f"the snapshot failed its integrity check: {integrity}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

            return {
                "status": "COMPLETE",
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "sha256": _sha256(target),
                "integrity": integrity,
                "created_at": moment.astimezone(timezone.utc).isoformat(),
                "pruned": self.prune(),
            }
        finally:
            self._lock.release()

    def prune(self) -> list[str]:
        """Delete snapshots beyond the retention count. Touches nothing else."""
        snapshots = self._snapshots()
        removed = []
        for stale in snapshots[self.retention:]:
            stale.unlink(missing_ok=True)
            removed.append(stale.name)
        return removed

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "modified_at": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).isoformat(timespec="seconds"),
            }
            for path in self._snapshots()
        ]

    # -- internals -----------------------------------------------------------
    def _snapshots(self) -> list[Path]:
        """Newest first. Only files this module names."""
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob(f"{NAME_PREFIX}*{NAME_SUFFIX}"), reverse=True)

    def _snapshot(self, temporary: Path) -> str:
        """Copy via SQLite's backup API, then quick_check the copy."""
        # `closing` on every connection, deliberately: see the module docstring.
        with closing(sqlite3.connect(self.db_path)) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
                destination.commit()
        with closing(sqlite3.connect(temporary)) as check:
            row = check.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "no result"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
