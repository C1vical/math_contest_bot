from __future__ import annotations

import json
import sqlite3

import pytest

from mathbot.bot.app import install_snapshot
from mathbot.config import ConfigError, load_settings
from mathbot.render.storage import LocalStorage
from mathbot.snapshot import (
    POINTER_KEY,
    SnapshotError,
    _resolve,
    create_snapshot,
    delete_older_snapshots,
    fetch_snapshot,
    inspect_database,
    publish_snapshot,
)
from tests.conftest import add_problem


@pytest.fixture
def published(repo, tmp_path):
    """A database with one problem, snapshotted and published to local storage."""
    add_problem(repo)
    source = tmp_path / "db" / "problems.db"
    snapshot = create_snapshot(source, tmp_path / "snapshots")
    store = LocalStorage(tmp_path / "bucket")
    manifest = publish_snapshot(snapshot, store)
    return snapshot, manifest, tmp_path / "bucket" / POINTER_KEY


def test_snapshot_is_a_consistent_self_contained_copy(repo, tmp_path):
    add_problem(repo)  # the source connection is still open, in WAL mode
    snapshot = create_snapshot(tmp_path / "db" / "problems.db", tmp_path / "snapshots")
    assert snapshot.problems == 1 and snapshot.schema_version >= 1 and len(snapshot.sha256) == 64
    assert not snapshot.path.with_name(snapshot.path.name + "-wal").exists()
    assert inspect_database(snapshot.path) == (snapshot.schema_version, 1)


def test_publish_writes_snapshot_then_mutable_pointer(published, tmp_path):
    snapshot, manifest, pointer = published
    assert manifest["key"].startswith("db/problems-") and manifest["key"].endswith(f"{snapshot.sha256[:12]}.db")
    assert json.loads(pointer.read_text())["sha256"] == snapshot.sha256
    assert (tmp_path / "bucket" / manifest["key"]).read_bytes() == snapshot.path.read_bytes()


def test_fetch_installs_verifies_and_skips_when_current(published, tmp_path):
    _, manifest, pointer = published
    target = tmp_path / "bothost" / "problems.db"
    installed = fetch_snapshot(str(pointer), target)
    assert installed["sha256"] == manifest["sha256"]
    assert inspect_database(target)[1] == 1
    assert fetch_snapshot(str(pointer), target) is None  # already current: nothing downloaded


def test_previous_database_is_kept_with_its_wal(published, tmp_path):
    _, _, pointer = published
    target = tmp_path / "bothost" / "problems.db"
    target.parent.mkdir(parents=True)
    old = sqlite3.connect(target)
    old.execute("CREATE TABLE marker (x)")
    old.commit()
    old.close()
    target.with_name("problems.db-wal").write_bytes(b"stale wal")
    fetch_snapshot(str(pointer), target)
    assert (tmp_path / "bothost" / "problems.db.prev").is_file()
    assert (tmp_path / "bothost" / "problems.db.prev-wal").read_bytes() == b"stale wal"
    assert not target.with_name("problems.db-wal").exists()  # never paired with the new file


def test_hash_mismatch_leaves_the_existing_database_untouched(published, tmp_path):
    _, manifest, pointer = published
    (tmp_path / "bucket" / manifest["key"]).write_bytes(b"corrupted in transit")
    target = tmp_path / "bothost" / "problems.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    with pytest.raises(SnapshotError, match="SHA-256"):
        fetch_snapshot(str(pointer), target)
    assert target.read_bytes() == b"existing"
    assert not list(target.parent.glob("*.download"))


def test_invalid_pointer_and_non_database_are_rejected(tmp_path):
    bad = tmp_path / "latest.json"
    bad.write_text("{not json")
    with pytest.raises(SnapshotError, match="not a valid snapshot pointer"):
        fetch_snapshot(str(bad), tmp_path / "p.db")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"x" * 200)
    with pytest.raises(SnapshotError):
        inspect_database(junk)


def test_resolve_snapshot_location_from_pointer():
    assert (
        _resolve("https://img.example.org/db/latest.json", "db/problems-1.db")
        == "https://img.example.org/db/problems-1.db"
    )
    assert (
        _resolve("https://img.example.org/prefix/db/latest.json", "db/p.db") == "https://img.example.org/prefix/db/p.db"
    )


def test_bot_startup_falls_back_to_the_local_database(tmp_path):
    settings = load_settings(
        {"DATA_DIR": str(tmp_path), "DATABASE_SNAPSHOT_URL": str(tmp_path / "missing" / "latest.json")}
    )
    with pytest.raises(ConfigError, match="no local database"):
        install_snapshot(settings)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.database_path.write_bytes(b"local copy")
    install_snapshot(settings)  # logs a warning and keeps the local copy
    assert settings.database_path.read_bytes() == b"local copy"


def test_a_file_that_is_not_a_database_is_a_snapshot_error_and_leaves_no_partial_copy(tmp_path):
    junk = tmp_path / "problems.db"
    junk.write_bytes(b"x" * 4096)
    with pytest.raises(SnapshotError, match="cannot copy"):
        create_snapshot(junk, tmp_path / "snapshots")
    assert list((tmp_path / "snapshots").iterdir()) == []


def test_publish_db_reports_a_storage_failure_instead_of_crashing(tmp_path, monkeypatch, capsys):
    import argparse

    from mathbot.cli import database as cli_database
    from mathbot.render import storage as storage_module

    class Unreachable(LocalStorage):
        def put(self, key, data, content_type, *, cache_control=None):
            raise storage_module.StorageError("bucket unreachable")

    settings = load_settings({"DATA_DIR": str(tmp_path)})
    monkeypatch.setattr(storage_module, "build_storage", lambda _settings: Unreachable(tmp_path / "bucket"))
    assert cli_database.cmd_publish_db(argparse.Namespace(dry_run=False), settings) == 1
    assert "bucket unreachable" in capsys.readouterr().err


OLD_TIME, NEW_TIME = "20200101T000000Z", "29990101T000000Z"


def seed_snapshots(tmp_path):
    """Snapshots from earlier and later publishes, and files that only look similar."""
    bucket, local = tmp_path / "bucket" / "db", tmp_path / "snapshots"
    bucket.mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)
    old = [bucket / f"problems-{OLD_TIME}-0123456789ab.db", local / f"problems-{OLD_TIME}.db"]
    keep = [
        bucket / f"problems-{NEW_TIME}-0123456789ab.db",
        local / f"problems-{NEW_TIME}.db",
        bucket / "problems.db",
        bucket / f"problems-{OLD_TIME}-notes.txt",
        local / f"problems-{OLD_TIME}.db.bak",
        tmp_path / "bucket" / f"problems-{OLD_TIME}-0123456789ab.db",  # not under db/
    ]
    for path in old + keep:
        path.write_bytes(b"x")
    return old, keep


def test_only_older_snapshots_are_deleted(published, tmp_path):
    snapshot, manifest, pointer = published
    old, keep = seed_snapshots(tmp_path)
    store = LocalStorage(tmp_path / "bucket")
    assert delete_older_snapshots(snapshot, store) == 2
    assert not any(path.exists() for path in old)
    assert all(path.exists() for path in keep)
    assert snapshot.path.is_file() and pointer.is_file() and (tmp_path / "bucket" / manifest["key"]).is_file()
    assert fetch_snapshot(str(pointer), tmp_path / "bothost" / "problems.db") is not None
    assert delete_older_snapshots(snapshot, store) == 0


def run_publish_db(tmp_path, monkeypatch, storage, *, dry_run=False):
    import argparse

    from mathbot.cli import database as cli_database
    from mathbot.render import storage as storage_module

    monkeypatch.setattr(storage_module, "build_storage", lambda _settings: storage)
    settings = load_settings({"DATA_DIR": str(tmp_path)})
    return cli_database.cmd_publish_db(argparse.Namespace(dry_run=dry_run), settings)


def test_publish_db_keeps_only_the_newest_snapshot(tmp_path, monkeypatch, capsys):
    old, keep = seed_snapshots(tmp_path)
    assert run_publish_db(tmp_path, monkeypatch, LocalStorage(tmp_path / "bucket")) == 0
    assert "older snapshots deleted: 2" in capsys.readouterr().out
    assert not any(path.exists() for path in old) and all(path.exists() for path in keep)


def test_nothing_is_deleted_by_a_dry_run_or_a_failed_publish(tmp_path, monkeypatch):
    from mathbot.render.storage import StorageError

    class PointerFails(LocalStorage):
        def put(self, key, data, content_type, *, cache_control=None):
            if key == POINTER_KEY:
                raise StorageError("bucket unreachable")
            super().put(key, data, content_type, cache_control=cache_control)

    old, keep = seed_snapshots(tmp_path)
    storage = PointerFails(tmp_path / "bucket")
    assert run_publish_db(tmp_path, monkeypatch, storage, dry_run=True) == 0
    assert run_publish_db(tmp_path, monkeypatch, storage) == 1
    assert all(path.exists() for path in old + keep)


def test_a_failed_deletion_does_not_fail_the_publish(tmp_path, monkeypatch, capsys):
    from mathbot.render.storage import StorageError

    class CannotDelete(LocalStorage):
        def delete(self, key):
            raise StorageError("access denied")

    seed_snapshots(tmp_path)
    assert run_publish_db(tmp_path, monkeypatch, CannotDelete(tmp_path / "bucket")) == 0
    captured = capsys.readouterr()
    assert "published to" in captured.out and "access denied" in captured.err
    assert (tmp_path / "bucket" / POINTER_KEY).is_file()


class StreamingResponse:
    """A streamed ``requests`` response that fails the test if the body is read in one piece."""

    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status_code = body, status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]

    @property
    def content(self):
        raise AssertionError("the snapshot must be streamed to disk, not read into memory")


def test_http_snapshot_is_streamed_to_disk_and_verified(published, tmp_path, monkeypatch):
    import mathbot.snapshot as snapshot_module

    _, manifest, pointer = published
    files = {
        "https://img.example.org/db/latest.json": pointer.read_bytes(),
        f"https://img.example.org/{manifest['key']}": (tmp_path / "bucket" / manifest["key"]).read_bytes(),
    }
    monkeypatch.setattr(snapshot_module, "CHUNK_BYTES", 1024)
    monkeypatch.setattr(snapshot_module.requests, "get", lambda url, **_: StreamingResponse(files[url]))
    target = tmp_path / "bothost" / "problems.db"
    assert fetch_snapshot("https://img.example.org/db/latest.json", target)["key"] == manifest["key"]
    assert inspect_database(target)[1] == 1


def test_oversized_snapshot_is_refused_and_leaves_nothing_behind(published, tmp_path, monkeypatch):
    import mathbot.snapshot as snapshot_module

    _, manifest, pointer = published
    body = (tmp_path / "bucket" / manifest["key"]).read_bytes()
    files = {"https://x.org/db/latest.json": pointer.read_bytes(), f"https://x.org/{manifest['key']}": body}
    monkeypatch.setattr(snapshot_module, "MAX_SNAPSHOT_BYTES", len(body) - 1)
    monkeypatch.setattr(snapshot_module.requests, "get", lambda url, **_: StreamingResponse(files[url]))
    target = tmp_path / "bothost" / "problems.db"
    with pytest.raises(SnapshotError, match="larger than"):
        fetch_snapshot("https://x.org/db/latest.json", target)
    assert list((tmp_path / "bothost").iterdir()) == []
