"""SQLite connections and schema migration."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mathbot.db.schema import LATEST_VERSION, MIGRATIONS

log = logging.getLogger(__name__)


def iso_utc(when: datetime | None = None) -> str:
    """UTC timestamp as stored in the database, e.g. ``2026-11-17T13:04:05Z``."""
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow() -> str:
    return iso_utc()


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection, creating the parent directory first."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: no implicit transactions; callers use explicit BEGIN/COMMIT.
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, each in its own transaction. Returns the schema version."""
    version = current_version(conn)
    if version > LATEST_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than this code understands ({LATEST_VERSION}); "
            "upgrade the bot or restore a matching database"
        )
    for target, sql in MIGRATIONS:
        if target <= version:
            continue
        log.info("applying migration", extra={"version": target})
        try:
            conn.execute("BEGIN")
            for statement in _split(sql):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (target, utcnow()))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        version = target
    return version


def _split(sql: str) -> list[str]:
    return [part.strip() for part in sql.split(";") if part.strip()]


def open_database(path: Path | str) -> sqlite3.Connection:
    """Connect and bring the schema up to date. A legacy ``math_problems`` table is left untouched."""
    conn = connect(path)
    migrate(conn)
    return conn
