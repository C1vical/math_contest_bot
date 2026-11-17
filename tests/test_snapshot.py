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
    fetch_snapshot,
    inspect_database,
    publish_snapshot,
)
from tests.test_render import add_problem


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
