"""contests, discover, ingest / refresh, cache-put, import-legacy: getting problems into the database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mathbot.cli import common
from mathbot.config import Settings
from mathbot.contests import PROVIDER_NAMES


def cmd_contests(args: argparse.Namespace, settings: Settings) -> int:
    registry = common.registry(settings)
    for series in registry.all(include_disabled=True):
        state = "enabled " if series.enabled else "DISABLED"
        print(f"{series.key:<11} {state} {series.family:<7} {PROVIDER_NAMES[series.provider]:<30} {series.name}")
        if series.notes and args.verbose:
            print(f"{'':<11} {series.notes}")
    return 0


def cmd_discover(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.ingest import Ingestor
    from mathbot.sources import build_adapters

    registry = common.registry(settings)
    repo = common.repo(settings, registry)
    ingestor = Ingestor(repo, build_adapters(common.client(settings), refresh=args.refresh))
    found, report = ingestor.discover(common.select(registry, args.contests))
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

    registry = common.registry(settings)
    repo = common.repo(settings, registry)
    selection = common.select(registry, args.contests)
    mode = "full" if args.full else "incremental"
    ingestor = Ingestor(
        repo,
        build_adapters(common.client(settings), refresh=args.full),
        concurrency=args.concurrency or settings.ingest_concurrency,
        progress=common.say,
    )
    report = ingestor.run(
        selection,
        years=common.int_set(args.year),
        numbers=common.int_set(args.number),
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


def cmd_cache_put(args: argparse.Namespace, settings: Settings) -> int:
    with open(args.file, "rb") as handle:
        data = handle.read()
    if not data.strip():
        print("refusing to cache an empty file", file=sys.stderr)
        return 2
    common.client(settings).seed_cache(args.url, data, args.content_type)
    print(f"cached {len(data)} bytes for {args.url}")
    return 0


def cmd_import_legacy(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.legacy import import_legacy

    registry = common.registry(settings)
    repo = common.repo(settings, registry)
    source = Path(args.database) if args.database else settings.database_path
    try:
        report = import_legacy(repo, registry, source, dry_run=args.dry_run, include_disabled=args.include_disabled)
    except (FileNotFoundError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.format())
    return 1 if report.unmapped else 0
