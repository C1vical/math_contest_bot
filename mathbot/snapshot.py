"""Publish the problem database for the bot host, and fetch it there at startup.

Ingestion and rendering run where Chromium is available; the bot may run
somewhere else. ``publish-db`` copies the database with SQLite's online backup
API (consistent even while other connections are open), checks the copy, and
uploads it next to the images:

    db/problems-<UTC timestamp>-<sha256[:12]>.db   immutable snapshot
    db/latest.json                                  pointer, re-written each publish

``latest.json`` holds ``key``, ``sha256``, ``size_bytes``, ``schema_version``,
``problems`` and ``created_at``. On the bot host, ``DATABASE_SNAPSHOT_URL``
(an http(s) URL or a local path to ``latest.json``) makes startup download the
snapshot it names, verify the hash and the database, and swap it in
atomically. The previous database is kept as ``<name>.prev``. If the download
fails, the bot keeps using the database it already has.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from mathbot.db.schema import LATEST_VERSION
from mathbot.http import atomic_write_bytes
from mathbot.render.storage import Storage

log = logging.getLogger(__name__)

SNAPSHOT_PREFIX = "db"
POINTER_KEY = f"{SNAPSHOT_PREFIX}/latest.json"
DOWNLOAD_TIMEOUT = 120
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024
MAX_POINTER_BYTES = 64 * 1024
CHUNK_BYTES = 1 << 20
_SIDECARS = ("-wal", "-shm")


class SnapshotError(Exception):
    """A snapshot could not be created, published, fetched or verified."""


@dataclass(frozen=True)
class Snapshot:
    path: Path
    sha256: str
    size_bytes: int
    schema_version: int
    problems: int
    created_at: str

    def manifest(self, key: str) -> dict:
        return {
            "key": key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "schema_version": self.schema_version,
            "problems": self.problems,
            "created_at": self.created_at,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(path: Path) -> tuple[int, int]:
    """Check a database file is intact and in our schema. Returns ``(schema version, problems)``."""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SnapshotError(f"cannot open {path}: {exc}") from exc
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SnapshotError(f"integrity check failed for {path}: {integrity}")
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        problems = conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    except sqlite3.Error as exc:
        raise SnapshotError(f"{path} is not a mathbot database: {exc}") from exc
    finally:
        conn.close()
    if version > LATEST_VERSION:
        raise SnapshotError(f"{path} has schema version {version}; this code understands up to {LATEST_VERSION}")
    return version, problems


def create_snapshot(database: Path, out_dir: Path) -> Snapshot:
    """Consistent single-file copy of ``database`` (WAL folded in) written to ``out_dir``."""
    if not database.is_file():
        raise SnapshotError(f"no database at {database}")
    out_dir.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"problems-{created}.db"
    tmp = target.with_suffix(".db.tmp")
    try:
        source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            dest = sqlite3.connect(tmp)
            try:
                source.backup(dest)
                dest.execute("PRAGMA journal_mode = DELETE")  # a self-contained file with no -wal sidecar
            finally:
                dest.close()
        finally:
            source.close()
    except sqlite3.Error as exc:
        tmp.unlink(missing_ok=True)
        raise SnapshotError(f"cannot copy {database}: {exc}") from exc
    except BaseException:  # Ctrl+C: leave no partial copy behind
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)
    version, problems = inspect_database(target)
    return Snapshot(target, _sha256_file(target), target.stat().st_size, version, problems, created)


def publish_snapshot(snapshot: Snapshot, storage: Storage) -> dict:
    """Upload the snapshot, then (only after that succeeded) move the ``latest.json`` pointer."""
    key = f"{SNAPSHOT_PREFIX}/problems-{snapshot.created_at}-{snapshot.sha256[:12]}.db"
    storage.put(key, snapshot.path.read_bytes(), "application/vnd.sqlite3")
    manifest = snapshot.manifest(key)
    storage.put(
        POINTER_KEY,
        json.dumps(manifest, indent=2).encode("utf-8"),
        "application/json",
        cache_control="no-cache, max-age=0",
    )
    return manifest


# --------------------------------------------------------------------------- bot host side


def _is_url(location: str) -> bool:
    return urlsplit(location).scheme in {"http", "https"}


def _local_path(location: str) -> Path:
    return Path(location.removeprefix("file://"))


def _chunks(location: str, limit: int):
    """Yield the bytes at ``location`` (http(s) URL or local path) in chunks, at most ``limit`` in total."""
    total = 0
    if _is_url(location):
        try:
            response = requests.get(location, timeout=DOWNLOAD_TIMEOUT, stream=True)
        except requests.RequestException as exc:
            raise SnapshotError(f"download of {location} failed: {exc}") from exc
        with response:
            if response.status_code != 200:
                raise SnapshotError(f"download of {location} failed: HTTP {response.status_code}")
            try:
                for chunk in response.iter_content(CHUNK_BYTES):
                    total += len(chunk)
                    if total > limit:
                        raise SnapshotError(f"{location} is larger than {limit} bytes")
                    yield chunk
            except requests.RequestException as exc:
                raise SnapshotError(f"download of {location} failed: {exc}") from exc
        return
    path = _local_path(location)
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
                total += len(chunk)
                if total > limit:
                    raise SnapshotError(f"{location} is larger than {limit} bytes")
                yield chunk
    except OSError as exc:
        raise SnapshotError(f"cannot read {path}: {exc}") from exc


def _read(location: str) -> bytes:
    """The small ``latest.json`` pointer, in memory."""
    return b"".join(_chunks(location, MAX_POINTER_BYTES))


def _download(location: str, target: Path) -> str:
    """Stream ``location`` into ``target`` without holding it in memory; returns its SHA-256."""
    digest = hashlib.sha256()
    with target.open("wb") as handle:
        for chunk in _chunks(location, MAX_SNAPSHOT_BYTES):
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest()


def _resolve(pointer_location: str, key: str) -> str:
    """Where the snapshot named by ``key`` lives, relative to the pointer's base location."""
    if _is_url(pointer_location):
        base = pointer_location.removesuffix(POINTER_KEY)
        return urljoin(base if base.endswith("/") else base + "/", key)
    pointer = _local_path(pointer_location)
    root = pointer.parent.parent if pointer.parent.name == SNAPSHOT_PREFIX else pointer.parent
    return str(root / key)


def _replace_database(new_file: Path, database: Path) -> None:
    """Swap ``new_file`` in as ``database``, keeping the old one (and its WAL) as ``.prev``."""
    previous = database.with_name(database.name + ".prev")
    for suffix in ("", *_SIDECARS):
        Path(str(previous) + suffix).unlink(missing_ok=True)
    if database.exists():
        for suffix in ("", *_SIDECARS):
            current = Path(str(database) + suffix)
            if current.exists():
                os.replace(current, Path(str(previous) + suffix))
    os.replace(new_file, database)


def fetch_snapshot(pointer_location: str, database: Path) -> dict | None:
    """Install the snapshot named by ``latest.json`` unless it is already installed.

    Returns the manifest when a new database was installed, ``None`` when the
    local copy is already current. Raises :class:`SnapshotError` on any failure;
    the existing database is untouched in that case.
    """
    try:
        manifest = json.loads(_read(pointer_location))
        key, expected = str(manifest["key"]), str(manifest["sha256"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SnapshotError(f"{pointer_location} is not a valid snapshot pointer: {exc}") from exc

    marker = database.with_name(database.name + ".snapshot")
    if database.is_file() and marker.is_file() and marker.read_text(encoding="utf-8").strip() == expected:
        return None

    database.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=database.name + ".", suffix=".download", dir=database.parent)
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        actual = _download(_resolve(pointer_location, key), tmp)
        if actual != expected:
            raise SnapshotError(f"snapshot {key} has SHA-256 {actual}, pointer says {expected}")
        inspect_database(tmp)
        _replace_database(tmp, database)
    finally:
        tmp.unlink(missing_ok=True)
    atomic_write_bytes(marker, expected.encode("utf-8"))
    log.info("database snapshot installed", extra={"key": key, "problems": manifest.get("problems")})
    return manifest
