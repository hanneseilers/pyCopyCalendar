"""SQLite state: mirror mappings, per-source status, and run history.

Remote CalDAV and this database cannot form one distributed transaction
(spec section 16), so every write here happens *after* a remote outcome
is confirmed, in its own short transaction - never before, and never
batched across multiple remote calls - so an interrupted run converges
correctly on the next invocation instead of believing something happened
that didn't (or vice versa).
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_CREATE_SCHEMA_VERSION = """
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

_CREATE_SOURCE_CALENDAR = """
CREATE TABLE source_calendar (
    source_id TEXT PRIMARY KEY,
    calendar_url TEXT NOT NULL,
    sync_token TEXT,
    last_success_at TEXT,
    last_error TEXT
);
"""

_CREATE_MIRROR_MAPPING = """
CREATE TABLE mirror_mapping (
    instance_key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_calendar(source_id),
    source_href TEXT NOT NULL,
    source_uid TEXT NOT NULL,
    recurrence_key TEXT NOT NULL,
    source_etag TEXT,
    target_href TEXT NOT NULL UNIQUE,
    target_uid TEXT NOT NULL UNIQUE,
    target_etag TEXT,
    fingerprint TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'missing', 'quarantined', 'deleted')),
    last_seen_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_SYNC_RUN = """
CREATE TABLE sync_run (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'aborted', 'failed')),
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
);
"""

_MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (1, (_CREATE_SCHEMA_VERSION, _CREATE_SOURCE_CALENDAR, _CREATE_MIRROR_MAPPING, _CREATE_SYNC_RUN)),
]


class MigrationError(RuntimeError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_version(conn: sqlite3.Connection) -> int:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not tables:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def _backup_database(db_path: Path, backup_dir: Path, retention: int) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{db_path.name}.{timestamp}.bak"
    shutil.copy2(db_path, backup_path)

    existing = sorted(backup_dir.glob(f"{db_path.name}.*.bak"))
    for stale in existing[:-retention] if retention > 0 else []:
        stale.unlink(missing_ok=True)


def open_database(db_path: Path, *, backup_dir: Path, backup_retention: int = 10) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database and apply any pending
    migrations, backing up the existing file first (spec section 15)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    needs_backup = db_path.exists() and db_path.stat().st_size > 0

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    current = _current_version(conn)
    pending = [m for m in _MIGRATIONS if m[0] > current]
    if not pending:
        return conn

    if needs_backup:
        _backup_database(db_path, backup_dir, backup_retention)

    try:
        for version, statements in pending:
            conn.execute("BEGIN")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, _utc_now_iso()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception as exc:
        conn.close()
        raise MigrationError(
            f"Migration to schema version failed; the pre-migration backup in "
            f"{backup_dir} (if one was needed) was left untouched: {exc}"
        ) from exc

    return conn


class StateRepository:
    """Thin, explicit-transaction wrapper around the SQLite schema.

    Every mutating method opens and commits its own short transaction, so
    a crash between two calls never leaves a half-written multi-row
    change - only ever "the last confirmed remote outcome was recorded,
    or it wasn't".
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # -- source_calendar -------------------------------------------------

    def record_source_success(self, source_id: str, calendar_url: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO source_calendar (source_id, calendar_url, last_success_at, last_error)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(source_id) DO UPDATE SET
                    calendar_url = excluded.calendar_url,
                    last_success_at = excluded.last_success_at,
                    last_error = NULL
                """,
                (source_id, calendar_url, _utc_now_iso()),
            )

    def record_source_error(self, source_id: str, calendar_url: str, error: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO source_calendar (source_id, calendar_url, last_error)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    calendar_url = excluded.calendar_url,
                    last_error = excluded.last_error
                """,
                (source_id, calendar_url, error),
            )

    # -- mirror_mapping ----------------------------------------------------

    def get_mapping(self, instance_key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM mirror_mapping WHERE instance_key = ?", (instance_key,)
        ).fetchone()

    def all_active_mappings(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM mirror_mapping WHERE status = 'active'"
        ).fetchall()

    def record_write(
        self,
        *,
        instance_key: str,
        source_id: str,
        source_href: str,
        source_uid: str,
        recurrence_key: str,
        source_etag: str | None,
        target_href: str,
        target_uid: str,
        target_etag: str | None,
        fingerprint: str,
        start_utc: str,
        end_utc: str,
        run_id: str,
    ) -> None:
        """Record a confirmed create/update/recreate outcome. Called only
        after the remote write succeeded."""
        now = _utc_now_iso()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO mirror_mapping (
                    instance_key, source_id, source_href, source_uid, recurrence_key,
                    source_etag, target_href, target_uid, target_etag, fingerprint,
                    start_utc, end_utc, status, last_seen_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(instance_key) DO UPDATE SET
                    source_href = excluded.source_href,
                    source_etag = excluded.source_etag,
                    target_href = excluded.target_href,
                    target_etag = excluded.target_etag,
                    fingerprint = excluded.fingerprint,
                    start_utc = excluded.start_utc,
                    end_utc = excluded.end_utc,
                    status = 'active',
                    last_seen_run_id = excluded.last_seen_run_id,
                    updated_at = excluded.updated_at
                """,
                (
                    instance_key, source_id, source_href, source_uid, recurrence_key,
                    source_etag, target_href, target_uid, target_etag, fingerprint,
                    start_utc, end_utc, run_id, now, now,
                ),
            )

    def record_deleted(self, instance_key: str) -> None:
        """Record a confirmed target deletion (or confirmed already-absent
        managed object)."""
        with self._conn:
            self._conn.execute(
                "UPDATE mirror_mapping SET status = 'deleted', updated_at = ? WHERE instance_key = ?",
                (_utc_now_iso(), instance_key),
            )

    def record_quarantined(self, instance_key: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE mirror_mapping SET status = 'quarantined', updated_at = ? WHERE instance_key = ?",
                (_utc_now_iso(), instance_key),
            )

    # -- sync_run ------------------------------------------------------

    def start_run(self, run_id: str, *, window_start: str, window_end: str, dry_run: bool) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO sync_run (run_id, started_at, window_start, window_end, dry_run, status)
                VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (run_id, _utc_now_iso(), window_start, window_end, int(dry_run)),
            )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        created: int = 0,
        updated: int = 0,
        deleted: int = 0,
        unchanged: int = 0,
        skipped: int = 0,
        failed: int = 0,
        error_summary: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                UPDATE sync_run SET
                    finished_at = ?, status = ?, created_count = ?, updated_count = ?,
                    deleted_count = ?, unchanged_count = ?, skipped_count = ?, failed_count = ?,
                    error_summary = ?
                WHERE run_id = ?
                """,
                (_utc_now_iso(), status, created, updated, deleted, unchanged, skipped, failed,
                 error_summary, run_id),
            )
