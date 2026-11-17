"""Command line interface: ``python -m mathbot <command>``.

    contests                    list the contest registry
    discover [CONTEST ...]      list the editions each source offers (no writes)
    ingest CONTEST ...          ingest selected contests / families / sources
    refresh                     ingest every enabled contest
    validate                    check database integrity and missing renders
    coverage                    what is missing: images, years, problem numbers, AMC choices
    prefetch-assets             download statement images ahead of rendering (resumable)
    render                      render images for new or changed problems
    upload                      push rendered images to object storage
    cleanup                     delete stale render files and records
    stats                       problem counts per contest
    cache-put URL FILE          seed the HTTP cache with a page you saved yourself
    install-render-assets       download the pinned MathJax / font files rendering needs
    import-legacy [DB]          import problems from a pre-2.0 math_problems table
    publish-db                  upload a database snapshot for the bot host
    pull-db [POINTER]           install the newest published snapshot here
    migrate                     create / upgrade the database schema
    bot                         run the Discord bot

CONTEST accepts a series (``AMC10A``, ``"aime ii"``), a family (``PCF``,
``FGH``, ``AMC``) or a source (``cemc``, ``aops_wiki``, ``aops_community``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import replace

from mathbot.config import ConfigError, Settings, load_settings
from mathbot.contests import PROVIDER_NAMES, ContestRegistry, ContestSeries, UnknownContestError
from mathbot.db import Repository, open_database
from mathbot.http import PoliteClient
from mathbot.logging_setup import configure_logging

log = logging.getLogger("mathbot.cli")


def _registry(settings: Settings) -> ContestRegistry:
    return ContestRegistry(enable=settings.enable_contests, disable=settings.disable_contests)


def _repo(settings: Settings, registry: ContestRegistry) -> Repository:
    repo = Repository(open_database(settings.database_path))
    repo.sync_registry(registry)
    return repo


def _client(settings: Settings) -> PoliteClient:
    return PoliteClient(settings.http, settings.http_cache_dir)


def _asset_client(settings: Settings) -> PoliteClient:
    """Client for statement images: paced by ASSET_MIN_INTERVAL; images are cached by the asset cache."""
    return PoliteClient(replace(settings.http, min_interval=settings.http.asset_min_interval))


def _select(registry: ContestRegistry, names: Sequence[str]) -> list[ContestSeries]:
    if not names:
        return registry.all()
    selected: dict[str, ContestSeries] = {}
    for name in names:
        for series in registry.select(name):
            selected[series.key] = series
    return list(selected.values())


def _int_set(values: Sequence[str] | None) -> frozenset[int] | None:
    """Parse ``2019 2021-2023`` style arguments."""
    if not values:
        return None
    out: set[int] = set()
    for value in values:
        for part in value.split(","):
            if "-" in part:
                low, high = part.split("-", 1)
                out.update(range(int(low), int(high) + 1))
            elif part:
                out.add(int(part))
    return frozenset(out)


def _say(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------- commands


def cmd_contests(args: argparse.Namespace, settings: Settings) -> int:
    registry = _registry(settings)
    for series in registry.all(include_disabled=True):
        state = "enabled " if series.enabled else "DISABLED"
        print(f"{series.key:<11} {state} {series.family:<7} {PROVIDER_NAMES[series.provider]:<30} {series.name}")
        if series.notes and args.verbose:
            print(f"{'':<11} {series.notes}")
    return 0


def cmd_discover(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.ingest import Ingestor
    from mathbot.sources import build_adapters

    registry = _registry(settings)
    repo = _repo(settings, registry)
    ingestor = Ingestor(repo, build_adapters(_client(settings), refresh=args.refresh))
    found, report = ingestor.discover(_select(registry, args.contests))
    for key, refs in found.items():
        years = [r.year for r in refs]
        fallback = sum(1 for r in refs if r.origin != "discovered")
        note = f"  ({fallback} from fallback data, not the live source)" if fallback else ""
        print(f"{key}: {len(refs)} edition(s), {min(years)}-{max(years)}{note}")
        if args.verbose:
            for ref in refs:
                print(f"    {ref.year}  {ref.source_url}  [{ref.origin}]")
    for provider, reason in report.blocked_providers.items():
        print(f"BLOCKED {provider}: {reason}")
    for error in report.errors:
        print(f"[{error.stage}] {error.series_key}: {error.error}")
    return 0 if report.ok else 1


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.ingest import Ingestor
    from mathbot.sources import build_adapters

    registry = _registry(settings)
    repo = _repo(settings, registry)
    selection = _select(registry, args.contests)
    mode = "full" if args.full else "incremental"
    ingestor = Ingestor(
        repo,
        build_adapters(_client(settings), refresh=args.full),
        concurrency=args.concurrency or settings.ingest_concurrency,
        progress=_say,
    )
    report = ingestor.run(
        selection,
        years=_int_set(args.year),
        numbers=_int_set(args.number),
        mode=mode,
        dry_run=args.dry_run,
        resume=args.resume,
    )
    print(report.format())
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report.to_dict(), handle, indent=2)
        print(f"report written to {args.report}")
    return 0 if report.ok else 1


def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.template import renderer_version
    from mathbot.validate import validate

    repo = _repo(settings, _registry(settings))
    report = validate(repo, settings.renders_dir, renderer_version(settings.render))
    print(report.format())
    if args.strict:
        return 0 if (report.ok and not report.warnings) else 1
    return 0 if report.ok else 1


def cmd_coverage(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    from mathbot.coverage import coverage
    from mathbot.render.template import renderer_version

    registry = _registry(settings)
    report = coverage(_repo(settings, registry), registry, renderer_version(settings.render))
    print(report.format())
    if args.csv:
        report.write_csv(Path(args.csv))
        print(f"\n{len(report.gaps):,} gap(s) written to {args.csv}")
    return 0


def _render_pipeline(settings: Settings, repo: Repository):
    from mathbot.render.assets import AssetCache
    from mathbot.render.pipeline import RenderPipeline

    assets = AssetCache(repo, _asset_client(settings), settings.assets_dir, settings.render.asset_ttl)
    return RenderPipeline(repo, settings.render, settings.renders_dir, assets, progress=_say)


def cmd_prefetch_assets(args: argparse.Namespace, settings: Settings) -> int:
    """Download statement images ahead of rendering, in resumable batches."""
    from urllib.parse import urlsplit

    from mathbot.render.assets import AssetError, remote_images

    registry = _registry(settings)
    repo = _repo(settings, registry)
    cache = _render_pipeline(settings, repo).assets
    assert cache is not None
    keys = [s.key for s in _select(registry, args.contests)] if args.contests else None

    wanted = list(dict.fromkeys(url for p in repo.iter_problems(keys) for url in remote_images(p.statement_html)))
    states = {url: cache.state(url) for url in wanted}
    pending = [url for url in wanted if states[url] == "missing"]
    failed = sum(1 for state in states.values() if state == "failed")
    interval = settings.http.asset_min_interval
    print(
        f"{len(wanted):,} distinct image(s): {len(wanted) - len(pending) - failed:,} cached, "
        f"{failed:,} failed recently (retried later), {len(pending):,} to fetch "
        f"(~{len(pending) * interval / 3600:.1f} h at one request per {interval:g} s, ASSET_MIN_INTERVAL)"
    )
    blocked = sorted({urlsplit(u).hostname or "" for u in pending} - settings.http.robots_override_hosts)
    if blocked:
        print(f"hosts not in ROBOTS_OVERRIDE_HOSTS (their robots.txt applies): {', '.join(blocked)}")
    if args.count:
        return 0
    batch = pending[: args.limit] if args.limit else pending
    fetched = errors = 0
    try:
        for index, url in enumerate(batch, start=1):
            try:
                cache.ensure(url)
                fetched += 1
            except AssetError:
                errors += 1
            if index % 100 == 0:
                print(f"  {index:,}/{len(batch):,}  ({fetched:,} cached, {errors:,} failed)", flush=True)
    except KeyboardInterrupt:
        print(f"interrupted: {fetched:,} cached, {errors:,} failed; run again to continue", file=sys.stderr)
        return 130
    print(f"done: {fetched:,} cached, {errors:,} failed, {len(pending) - len(batch):,} left for later runs")
    return 0


def cmd_render(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.pipeline import PlaywrightRenderer

    registry = _registry(settings)
    repo = _repo(settings, registry)
    pipeline = _render_pipeline(settings, repo)
    keys = [s.key for s in _select(registry, args.contests)] if args.contests else None
    problems = pipeline.plan(keys, limit=args.limit)
    print(f"{len(problems)} problem(s) need rendering (renderer {pipeline.version})")
    if args.dry_run or not problems:
        return 0

    failures = pipeline.prefetch(problems)  # remote images: blocking I/O before the async phase
    if failures:
        print(f"{len(failures)} problem(s) have images that could not be downloaded")

    async def run():
        async with PlaywrightRenderer(settings.render, settings.render_assets_dir) as renderer:
            return await pipeline.render(problems, renderer, prefetch_failures=failures)

    report = asyncio.run(run())
    print(report.format())
    return 0 if report.failed == 0 else 1


def cmd_upload(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.storage import build_storage

    repo = _repo(settings, _registry(settings))
    if settings.storage.backend == "local":
        # The bot serves RENDERS_DIR directly; a second copy would only duplicate it.
        report = _render_pipeline(settings, repo).upload(None, force=args.force)
        print(f"storage backend: local (images are served from {settings.renders_dir}; nothing is copied)")
    else:
        storage = build_storage(settings)
        report = _render_pipeline(settings, repo).upload(storage, force=args.force)
        print(f"storage backend: {storage.name}")
    print(report.format())
    return 0 if report.failed == 0 else 1


def cmd_cleanup(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.storage import build_storage

    repo = _repo(settings, _registry(settings))
    # Local renders have no remote copy: deleting the local file is all there is to do.
    storage = build_storage(settings) if args.remote and settings.storage.backend != "local" else None
    counts = _render_pipeline(settings, repo).cleanup(storage, dry_run=args.dry_run)
    prefix = "would remove" if args.dry_run else "removed"
    print(f"{prefix}: " + ", ".join(f"{value} {name.replace('_', ' ')}" for name, value in counts.items()))
    return 0


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    repo = _repo(settings, _registry(settings))
    print(f"{'contest':<12}{'source':<16}{'editions':>9}{'problems':>9}  years")
    for s in repo.series_stats():
        years = f"{s.first_year}-{s.last_year}" if s.problems else "-"
        print(f"{s.key:<12}{s.provider:<16}{s.editions:>9}{s.problems:>9}  {years}")
    print(", ".join(f"{name}: {value}" for name, value in repo.totals().items()))
    return 0


def cmd_cache_put(args: argparse.Namespace, settings: Settings) -> int:
    with open(args.file, "rb") as handle:
        data = handle.read()
    if not data.strip():
        print("refusing to cache an empty file", file=sys.stderr)
        return 2
    _client(settings).seed_cache(args.url, data, args.content_type)
    print(f"cached {len(data)} bytes for {args.url}")
    return 0


def cmd_install_render_assets(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.vendor import RenderAssetsMissing, install_assets

    try:
        outcomes = install_assets(settings.render_assets_dir, _client(settings))
    except RenderAssetsMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path, outcome in outcomes.items():
        print(f"{outcome:>9}  {path}")
    print(f"render assets ready in {settings.render_assets_dir}")
    return 0


def cmd_import_legacy(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    from mathbot.legacy import import_legacy

    registry = _registry(settings)
    repo = _repo(settings, registry)
    source = Path(args.database) if args.database else settings.database_path
    try:
        report = import_legacy(repo, registry, source, dry_run=args.dry_run, include_disabled=args.include_disabled)
    except (FileNotFoundError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.format())
    return 1 if report.unmapped else 0


def cmd_publish_db(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.storage import build_storage
    from mathbot.snapshot import SnapshotError, create_snapshot, publish_snapshot

    _repo(settings, _registry(settings))  # make sure the schema is current before copying it
    try:
        snapshot = create_snapshot(settings.database_path, settings.data_dir / "snapshots")
        print(f"snapshot {snapshot.path.name}: {snapshot.problems} problems, {snapshot.size_bytes:,} bytes")
        if args.dry_run:
            return 0
        storage = build_storage(settings)
        manifest = publish_snapshot(snapshot, storage)
    except SnapshotError as exc:
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


def cmd_migrate(args: argparse.Namespace, settings: Settings) -> int:
    _repo(settings, _registry(settings))
    print(f"database ready at {settings.database_path}")
    return 0


def cmd_bot(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.bot.app import run_bot

    return run_bot(settings)


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mathbot", description="Math contest bot maintenance commands.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("contests", help="list the contest registry")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_contests)

    p = sub.add_parser("discover", help="list editions available at the sources (no writes)")
    p.add_argument("contests", nargs="*", help="series, family or source; default: all enabled")
    p.add_argument("--refresh", action="store_true", help="ignore cached listings")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_discover)

    def ingest_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--year", nargs="+", metavar="YEAR", help="years or ranges, e.g. 2019 2021-2023")
        p.add_argument("--number", nargs="+", metavar="N", help="problem numbers or ranges")
        p.add_argument("--full", action="store_true", help="full refresh: re-fetch everything, bypassing the cache")
        p.add_argument("--dry-run", action="store_true", help="fetch and parse but write nothing")
        p.add_argument("--resume", action="store_true", help="skip editions an interrupted run already finished")
        p.add_argument("--concurrency", type=int, help="parallel edition fetches (default INGEST_CONCURRENCY)")
        p.add_argument("--report", metavar="FILE", help="also write the report as JSON")
        p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("ingest", help="ingest selected contests")
    p.add_argument("contests", nargs="+", help="series, family or source")
    ingest_options(p)

    p = sub.add_parser("refresh", help="ingest every enabled contest")
    p.set_defaults(contests=[])
    ingest_options(p)

    p = sub.add_parser("validate", help="check database integrity and missing renders")
    p.add_argument("--strict", action="store_true", help="treat warnings as failures")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("coverage", help="report missing images, years, problem numbers and AMC choices")
    p.add_argument("--csv", metavar="FILE", help="also write one row per gap to FILE")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("render", help="render images for new or changed problems")
    p.add_argument("contests", nargs="*")
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true", help="only report how many problems need rendering")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("prefetch-assets", help="download statement images before rendering (resumable)")
    p.add_argument("contests", nargs="*")
    p.add_argument("--limit", type=int, help="fetch at most N images this run")
    p.add_argument("--count", action="store_true", help="only report how many images are cached / pending")
    p.set_defaults(func=cmd_prefetch_assets)

    p = sub.add_parser("upload", help="upload rendered images that are not in object storage yet")
    p.add_argument("--force", action="store_true", help="re-upload everything")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("cleanup", help="delete stale render files and records")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--remote", action="store_true", help="also delete stale objects from object storage")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("stats", help="problem counts per contest")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("cache-put", help="seed the HTTP cache with a page you saved yourself")
    p.add_argument("url")
    p.add_argument("file")
    p.add_argument("--content-type", default="text/html; charset=utf-8")
    p.set_defaults(func=cmd_cache_put)

    p = sub.add_parser("install-render-assets", help="download the pinned MathJax / font files")
    p.set_defaults(func=cmd_install_render_assets)

    p = sub.add_parser("import-legacy", help="import problems from a pre-2.0 math_problems table")
    p.add_argument("database", nargs="?", help="old database file (default: DATABASE_PATH)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--include-disabled", action="store_true", help="also import contests that are disabled")
    p.set_defaults(func=cmd_import_legacy)

    p = sub.add_parser("publish-db", help="upload a consistent database snapshot for the bot host")
    p.add_argument("--dry-run", action="store_true", help="create and check the snapshot but do not upload")
    p.set_defaults(func=cmd_publish_db)

    p = sub.add_parser("pull-db", help="install the newest published database snapshot")
    p.add_argument("source", nargs="?", help="pointer URL or path (default: DATABASE_SNAPSHOT_URL)")
    p.set_defaults(func=cmd_pull_db)

    p = sub.add_parser("migrate", help="create / upgrade the database schema")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("bot", help="run the Discord bot")
    p.set_defaults(func=cmd_bot)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        return args.func(args, settings)
    except (ConfigError, UnknownContestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
