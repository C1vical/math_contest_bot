"""validate, coverage, stats, migrate, publish-db, pull-db: checking and shipping the database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mathbot.cli import common
from mathbot.config import Settings


def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.template import renderer_version
    from mathbot.validate import validate

    repo = common.repo(settings, common.registry(settings))
    report = validate(repo, settings.renders_dir, renderer_version(settings.render))
    print(report.format())
    if args.strict:
        return 0 if (report.ok and not report.warnings) else 1
    return 0 if report.ok else 1


def cmd_coverage(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.coverage import coverage
    from mathbot.render.template import renderer_version

    registry = common.registry(settings)
    report = coverage(common.repo(settings, registry), registry, renderer_version(settings.render))
    print(report.format())
    if args.csv:
        report.write_csv(Path(args.csv))
        print(f"\n{len(report.gaps):,} gap(s) written to {args.csv}")
    return 0


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    repo = common.repo(settings, common.registry(settings))
    print(f"{'contest':<12}{'source':<16}{'editions':>9}{'problems':>9}  years")
    for s in repo.series_stats():
        years = f"{s.first_year}-{s.last_year}" if s.problems else "-"
        print(f"{s.key:<12}{s.provider:<16}{s.editions:>9}{s.problems:>9}  {years}")
    print(", ".join(f"{name}: {value}" for name, value in repo.totals().items()))
    return 0


def cmd_migrate(args: argparse.Namespace, settings: Settings) -> int:
    common.repo(settings, common.registry(settings))
    print(f"database ready at {settings.database_path}")
    return 0


def cmd_publish_db(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.storage import StorageError, build_storage
    from mathbot.snapshot import SnapshotError, create_snapshot, publish_snapshot

    common.repo(settings, common.registry(settings))  # make sure the schema is current before copying it
    try:
        snapshot = create_snapshot(settings.database_path, settings.data_dir / "snapshots")
        print(f"snapshot {snapshot.path.name}: {snapshot.problems} problems, {snapshot.size_bytes:,} bytes")
        if args.dry_run:
            return 0
        storage = build_storage(settings)
        manifest = publish_snapshot(snapshot, storage)
    except (SnapshotError, StorageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"published to {storage.name} storage as {manifest['key']} (pointer: db/latest.json)")
    return 0


def cmd_pull_db(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.snapshot import SnapshotError, fetch_snapshot

    location = args.source or settings.database_snapshot_url
    if not location:
        print("error: give a pointer location or set DATABASE_SNAPSHOT_URL", file=sys.stderr)
        return 2
    try:
        manifest = fetch_snapshot(location, settings.database_path)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if manifest is None:
        print(f"{settings.database_path} already matches the published snapshot")
    else:
        print(f"installed {manifest['key']} ({manifest.get('problems')} problems) at {settings.database_path}")
    return 0
