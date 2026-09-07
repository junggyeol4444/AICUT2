"""The operational layer ported from the Codex build (`legacy/backend/`).

Four things the engine needed and did not have: a key on the API surface, the
cached OAuth token encrypted at rest, database snapshots, and something to tick
while `aicut ui` is up. Each test here pins the behaviour that made the feature
worth porting, and the two places the original leaked.
"""

import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from aicut.db.backup import BackupError, DatabaseBackup
from aicut.db.store import Store
from aicut.errors import AicutError
from aicut.intelligence.token_store import EncryptedTokenStore, TokenStoreError, store_from_environment
from aicut.scheduler import Periodic, Scheduler
from aicut.ui.auth import ApiKeyGuard, guard_from_environment
from aicut.ui.server import serve


# ---------------------------------------------------------------------------
# 1. API key (18장 "서버")
# ---------------------------------------------------------------------------
class ApiKeyGuardTests(unittest.TestCase):
    def test_no_key_configured_lets_everything_through(self):
        """The plan calls this a single-user local tool; the key is opt-in."""
        guard = ApiKeyGuard(None)
        self.assertFalse(guard.enabled)
        self.assertTrue(guard.authorized("/api/projects", {}))
        self.assertTrue(guard.authorized("/", {}))

    def test_blank_key_is_not_a_key(self):
        self.assertFalse(ApiKeyGuard("   ").enabled)

    def test_api_paths_need_the_key(self):
        guard = ApiKeyGuard("s3cret")
        self.assertFalse(guard.authorized("/api/projects", {}))
        self.assertTrue(guard.authorized("/api/projects", {"Authorization": "Bearer s3cret"}))

    def test_malformed_authorization_headers_are_refused(self):
        guard = ApiKeyGuard("s3cret")
        for header in ("", "s3cret", "Bearer", "Bearer ", "Basic s3cret", "Bearer wrong",
                       "Bearer s3cre", "Bearer s3crett"):
            with self.subTest(header=header):
                self.assertFalse(guard.authorized("/api/projects", {"Authorization": header}))

    def test_scheme_is_case_insensitive_but_the_key_is_not(self):
        guard = ApiKeyGuard("s3cret")
        self.assertTrue(guard.authorized("/api/x", {"Authorization": "bearer s3cret"}))
        self.assertFalse(guard.authorized("/api/x", {"Authorization": "Bearer S3CRET"}))

    def test_health_is_exempt_so_a_probe_needs_no_secret(self):
        self.assertTrue(ApiKeyGuard("s3cret").authorized("/api/health", {}))

    def test_environment_switch(self):
        self.assertFalse(guard_from_environment({}).enabled)
        self.assertTrue(guard_from_environment({"AICUT_UI_API_KEY": "k"}).enabled)


class UiAuthTests(unittest.TestCase):
    """The regression the Codex build shipped: a key that guarded only the JSON."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.workspace = Path(cls._tmp.name)
        # Something secret in the workspace, to prove the static route cannot
        # reach it. In the original, static's document root was the project
        # directory, so this shape came back with 200 and no key.
        (cls.workspace / "token.json").write_text("REFRESH-TOKEN-SECRET", encoding="utf-8")
        cls.httpd, cls.ui = serve(cls.workspace, port=0, guard=ApiKeyGuard("s3cret"))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.ui.close()
        cls._tmp.cleanup()

    def _get(self, path: str, key: str | None = None):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_api_is_401_without_the_key(self):
        status, _ = self._get("/api/projects")
        self.assertEqual(status, 401)

    def test_api_is_200_with_the_key(self):
        status, body = self._get("/api/projects", "s3cret")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)

    def test_401_names_the_scheme(self):
        req = urllib.request.Request(self.base + "/api/projects")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertIn("Bearer", caught.exception.headers.get("WWW-Authenticate", ""))

    def test_health_answers_without_the_key(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_the_page_is_served_without_a_key(self):
        """A browser must fetch the page before it can send any header."""
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())

    def test_the_workspace_is_not_reachable_through_the_static_route(self):
        """The hole in the original: static's root was the whole directory."""
        for path in ("/token.json", "/aicut.db", "/../token.json", "/%2e%2e/token.json",
                     "/../../etc/passwd", "/pyproject.toml"):
            with self.subTest(path=path):
                status, body = self._get(path)
                self.assertEqual(status, 404, f"{path} was served")
                self.assertNotIn(b"REFRESH-TOKEN-SECRET", body)
                self.assertNotIn(b"root:", body)

    def test_runtime_endpoint_reports_that_the_key_is_on(self):
        status, body = self._get("/api/runtime", "s3cret")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["auth"]["api_key_required"])


# ---------------------------------------------------------------------------
# 2. OAuth token at rest
# ---------------------------------------------------------------------------
class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "token.json.enc"
        self.document = {"refresh_token": "1//long-lived-grant", "client_id": "abc", "scopes": ["a"]}

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip(self):
        store = EncryptedTokenStore(self.path, "passphrase")
        store.save(self.document)
        self.assertEqual(EncryptedTokenStore(self.path, "passphrase").load(), self.document)

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(EncryptedTokenStore(self.path, "passphrase").load())

    def test_no_passphrase_is_refused_loudly(self):
        with self.assertRaises(TokenStoreError):
            EncryptedTokenStore(self.path, "")

    def test_the_refresh_token_is_not_on_disk_in_the_clear(self):
        """The whole point: this file is a standing grant to the channel."""
        EncryptedTokenStore(self.path, "passphrase").save(self.document)
        raw = self.path.read_bytes()
        self.assertNotIn(b"1//long-lived-grant", raw)
        self.assertNotIn(b"refresh_token", raw)

    def test_wrong_passphrase_is_refused_and_says_so(self):
        EncryptedTokenStore(self.path, "passphrase").save(self.document)
        with self.assertRaises(TokenStoreError) as caught:
            EncryptedTokenStore(self.path, "different").load()
        self.assertIn("passphrase", str(caught.exception))

    def test_tampering_is_detected(self):
        """Encrypt-then-MAC: a flipped ciphertext bit must not decrypt."""
        EncryptedTokenStore(self.path, "passphrase").save(self.document)
        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        body = list(envelope["ciphertext"])
        body[0] = "B" if body[0] != "B" else "C"
        envelope["ciphertext"] = "".join(body)
        self.path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(TokenStoreError):
            EncryptedTokenStore(self.path, "passphrase").load()

    def test_a_future_version_is_refused_rather_than_misread(self):
        EncryptedTokenStore(self.path, "passphrase").save(self.document)
        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        envelope["version"] = 99
        self.path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(TokenStoreError) as caught:
            EncryptedTokenStore(self.path, "passphrase").load()
        self.assertIn("99", str(caught.exception))

    def test_garbage_is_reported_not_swallowed(self):
        self.path.write_text("this is not json", encoding="utf-8")
        with self.assertRaises(TokenStoreError):
            EncryptedTokenStore(self.path, "passphrase").load()

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_file_is_owner_only_from_the_moment_it_exists(self):
        """Opened at 0600 rather than chmod'ed after the write."""
        EncryptedTokenStore(self.path, "passphrase").save(self.document)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_no_temporary_file_survives_a_save(self):
        EncryptedTokenStore(self.path, "passphrase").save(self.document)
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_every_save_uses_a_fresh_salt_and_nonce(self):
        store = EncryptedTokenStore(self.path, "passphrase")
        store.save(self.document)
        first = json.loads(self.path.read_text(encoding="utf-8"))
        store.save(self.document)
        second = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertNotEqual(first["ciphertext"], second["ciphertext"])

    def test_documents_longer_than_one_block_survive(self):
        """The keystream is chunked; an off-by-one there would truncate."""
        for size in (0, 1, 31, 32, 33, 64, 1000):
            with self.subTest(size=size):
                document = {"blob": "x" * size}
                EncryptedTokenStore(self.path, "passphrase").save(document)
                self.assertEqual(EncryptedTokenStore(self.path, "passphrase").load(), document)

    def test_environment_switch_is_opt_in(self):
        self.assertIsNone(store_from_environment(self.path, {}))
        self.assertIsNotNone(store_from_environment(self.path, {"AICUT_TOKEN_KEY": "k"}))

    def test_it_is_an_aicut_error(self):
        """So the CLI's top-level handler prints it instead of a traceback."""
        self.assertTrue(issubclass(TokenStoreError, AicutError))


# ---------------------------------------------------------------------------
# 3. Database snapshots
# ---------------------------------------------------------------------------
class BackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "aicut.db"
        self.store = Store(self.db_path)
        self.store.conn.execute("CREATE TABLE probe (v TEXT)")
        self.store.conn.execute("INSERT INTO probe VALUES ('사건 A')")
        self.store.conn.commit()
        self.backup = DatabaseBackup(self.db_path, self.root / "backups")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_a_snapshot_is_a_readable_database_with_the_data_in_it(self):
        result = self.backup.create()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["integrity"], "ok")
        conn = sqlite3.connect(result["path"])
        try:
            self.assertEqual(conn.execute("SELECT v FROM probe").fetchone()[0], "사건 A")
        finally:
            conn.close()

    def test_the_reported_sha256_matches_the_file(self):
        import hashlib
        result = self.backup.create()
        digest = hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest()
        self.assertEqual(result["sha256"], digest)

    def test_writes_after_the_snapshot_are_not_in_it(self):
        """It is a point-in-time image, which is what makes it a backup."""
        result = self.backup.create()
        self.store.conn.execute("INSERT INTO probe VALUES ('나중')")
        self.store.conn.commit()
        conn = sqlite3.connect(result["path"])
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM probe").fetchone()[0], 1)
        finally:
            conn.close()

    def test_retention_prunes_oldest_first(self):
        backup = DatabaseBackup(self.db_path, self.root / "backups", retention=3)
        moments = [datetime(2026, 9, 7, 1, minute, tzinfo=timezone.utc) for minute in range(5)]
        for moment in moments:
            backup.clock = lambda m=moment: m
            backup.create()
        names = [item["name"] for item in backup.list()]
        self.assertEqual(len(names), 3)
        self.assertTrue(all("T0103" in n or "T0104" in n or "T0102" in n for n in names), names)

    def test_pruning_touches_only_files_this_module_named(self):
        """A retention sweep must never take out something else in the directory."""
        directory = self.root / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        bystander = directory / "operator-notes.txt"
        bystander.write_text("do not delete", encoding="utf-8")
        backup = DatabaseBackup(self.db_path, directory, retention=1)
        for minute in range(3):
            backup.clock = lambda m=minute: datetime(2026, 9, 7, 2, m, tzinfo=timezone.utc)
            backup.create()
        self.assertTrue(bystander.is_file())
        self.assertEqual(bystander.read_text(encoding="utf-8"), "do not delete")

    def test_a_second_concurrent_snapshot_is_skipped_not_raced(self):
        self.backup._lock.acquire()
        try:
            self.assertEqual(self.backup.create()["status"], "SKIPPED")
        finally:
            self.backup._lock.release()

    def test_a_missing_database_is_an_error_not_an_empty_snapshot(self):
        self.store.close()
        self.db_path.unlink()
        (self.root / "aicut.db-wal").unlink(missing_ok=True)
        (self.root / "aicut.db-shm").unlink(missing_ok=True)
        with self.assertRaises(BackupError):
            self.backup.create()

    def test_retention_below_one_is_refused(self):
        with self.assertRaises(BackupError):
            DatabaseBackup(self.db_path, self.root / "backups", retention=0)

    def test_a_naive_clock_is_refused(self):
        """An ambiguous stamp would break the ordering the prune relies on."""
        self.backup.clock = lambda: datetime(2026, 9, 7, 3, 0)
        with self.assertRaises(BackupError):
            self.backup.create()

    def test_listing_an_absent_directory_is_empty_not_an_error(self):
        self.assertEqual(DatabaseBackup(self.db_path, self.root / "nope").list(), [])

    def test_no_temporary_file_survives(self):
        """The original left connections open; on Windows the publish then failed."""
        self.backup.create()
        leftovers = [p.name for p in (self.root / "backups").iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_names_sort_chronologically(self):
        """The prune sorts by name, so the format has to make that true."""
        backup = DatabaseBackup(self.db_path, self.root / "backups", retention=10)
        stamps = []
        for moment in (datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                       datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc),
                       datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)):
            backup.clock = lambda m=moment: m
            stamps.append(Path(backup.create()["path"]).name)
        self.assertEqual(stamps, sorted(stamps))


# ---------------------------------------------------------------------------
# 4. Scheduler
# ---------------------------------------------------------------------------
class PeriodicTests(unittest.TestCase):
    def test_it_gates_on_its_own_interval(self):
        """A daily job must not fire on every tick of a one-minute loop."""
        clock, calls = [0.0], []
        periodic = Periodic(lambda: calls.append(1), 100.0, clock=lambda: clock[0])
        self.assertEqual(periodic()["status"], "SKIPPED")
        clock[0] = 100.0
        periodic()
        self.assertEqual(len(calls), 1)
        clock[0] = 150.0
        self.assertEqual(periodic()["status"], "SKIPPED")
        clock[0] = 200.0
        periodic()
        self.assertEqual(len(calls), 2)

    def test_run_immediately_fires_on_the_first_tick(self):
        calls = []
        Periodic(lambda: calls.append(1), 100.0, run_immediately=True, clock=lambda: 0.0)()
        self.assertEqual(len(calls), 1)

    def test_a_skip_says_how_long_is_left(self):
        result = Periodic(lambda: None, 60.0, clock=lambda: 0.0)()
        self.assertEqual(result["due_in_sec"], 60.0)

    def test_a_non_positive_interval_is_refused(self):
        for bad in (0, -1):
            with self.subTest(interval=bad):
                with self.assertRaises(ValueError):
                    Periodic(lambda: None, bad)


class SchedulerTests(unittest.TestCase):
    def test_one_task_failing_does_not_stop_the_others(self):
        """A quota error draining uploads must not cost the backup."""
        def boom():
            raise RuntimeError("quota exceeded")

        results = Scheduler({"uploads": boom, "backup": lambda: "snapshot"}).run_once()
        self.assertEqual(results["uploads"]["status"], "FAILED")
        self.assertIn("quota exceeded", results["uploads"]["error"])
        self.assertEqual(results["backup"], {"status": "COMPLETE", "result": "snapshot"})

    def test_run_once_never_raises(self):
        Scheduler({"bad": lambda: (_ for _ in ()).throw(KeyboardInterrupt)})  # constructed only
        results = Scheduler({"bad": lambda: 1 / 0}).run_once()
        self.assertEqual(results["bad"]["status"], "FAILED")

    def test_an_overlapping_pass_is_skipped(self):
        scheduler = Scheduler({"x": lambda: 1})
        scheduler._run_lock.acquire()
        try:
            self.assertEqual(scheduler.run_once()["scheduler"]["status"], "SKIPPED")
        finally:
            scheduler._run_lock.release()

    def test_status_reports_the_last_pass(self):
        scheduler = Scheduler({"x": lambda: "done"})
        scheduler.run_once()
        status = scheduler.status()
        self.assertEqual(status["results"]["x"]["result"], "done")
        self.assertIsNotNone(status["last_run_at"])
        datetime.fromisoformat(status["last_run_at"])   # parses as ISO-8601

    def test_the_history_hook_receives_the_results(self):
        seen = {}
        scheduler = Scheduler(
            {"x": lambda: 1}, on_run=lambda results, at: seen.update(results=results, at=at),
        )
        scheduler.run_once()
        self.assertEqual(seen["results"]["x"]["result"], 1)

    def test_a_failing_history_hook_does_not_lose_the_work(self):
        def explode(results, at):
            raise OSError("disk full")

        scheduler = Scheduler({"x": lambda: 1}, on_run=explode)
        results = scheduler.run_once()
        self.assertEqual(results["x"]["status"], "COMPLETE")
        self.assertEqual(scheduler.status()["results"]["history"]["status"], "FAILED")

    def test_start_stop_is_idempotent_and_actually_runs(self):
        calls = []
        scheduler = Scheduler({"x": lambda: calls.append(1)}, interval_sec=0.05)
        self.assertTrue(scheduler.start())
        self.assertFalse(scheduler.start())
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        scheduler.stop(timeout=5)
        self.assertTrue(calls)
        self.assertFalse(scheduler.status()["running"])

    def test_a_non_positive_interval_is_refused(self):
        with self.assertRaises(ValueError):
            Scheduler({}, interval_sec=0)


if __name__ == "__main__":
    unittest.main()
