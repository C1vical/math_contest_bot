"""Command line interface: ``python -m mathbot <command>``.

    contests                    list the contest registry
    discover [CONTEST ...]      list the editions each source offers (no writes)
    ingest CONTEST ...          ingest selected contests / families / sources
    refresh                     ingest every enabled contest
    validate                    check database integrity and missing renders
    coverage                    what is missing: images, years, problem numbers, AMC choices
    prefetch-assets             download statement images ahead of rendering (resumable)
    import-assets [FOLDER]      add images you saved in your own browser (--html lists them)
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
import sys
from collections.abc import Sequence

from mathbot.cli import database, images, ingest
from mathbot.config import ConfigError, Settings, load_settings
from mathbot.contests import UnknownContestError
from mathbot.logging_setup import configure_logging


def cmd_bot(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.bot.app import run_bot

    return run_bot(settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mathbot", description="Math contest bot maintenance commands.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("contests", help="list the contest registry")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=ingest.cmd_contests)

    p = sub.add_parser("discover", help="list editions available at the sources (no writes)")
    p.add_argument("contests", nargs="*", help="series, family or source; default: all enabled")
    p.add_argument("--refresh", action="store_true", help="ignore cached listings")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=ingest.cmd_discover)

    def ingest_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--year", nargs="+", metavar="YEAR", help="years or ranges, e.g. 2019 2021-2023")
        p.add_argument("--number", nargs="+", metavar="N", help="problem numbers or ranges")
        p.add_argument("--full", action="store_true", help="full refresh: re-fetch everything, bypassing the cache")
        p.add_argument("--dry-run", action="store_true", help="fetch and parse but write nothing")
        p.add_argument("--resume", action="store_true", help="skip editions an interrupted run already finished")
        p.add_argument("--concurrency", type=int, help="parallel edition fetches (default INGEST_CONCURRENCY)")
        p.add_argument("--report", metavar="FILE", help="also write the report as JSON")
        p.set_defaults(func=ingest.cmd_ingest)

    p = sub.add_parser("ingest", help="ingest selected contests")
    p.add_argument("contests", nargs="+", help="series, family or source")
    ingest_options(p)

    p = sub.add_parser("refresh", help="ingest every enabled contest")
    p.set_defaults(contests=[])
    ingest_options(p)

    p = sub.add_parser("validate", help="check database integrity and missing renders")
    p.add_argument("--strict", action="store_true", help="treat warnings as failures")
    p.set_defaults(func=database.cmd_validate)

    p = sub.add_parser("coverage", help="report missing images, years, problem numbers and AMC choices")
    p.add_argument("--csv", metavar="FILE", help="also write one row per gap to FILE")
    p.set_defaults(func=database.cmd_coverage)

    p = sub.add_parser("render", help="render images for new or changed problems")
    p.add_argument("contests", nargs="*")
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true", help="only report how many problems need rendering")
    p.set_defaults(func=images.cmd_render)

    p = sub.add_parser("prefetch-assets", help="download statement images before rendering (resumable)")
    p.add_argument("contests", nargs="*")
    p.add_argument("--limit", type=int, help="fetch at most N images this run")
    p.add_argument("--count", action="store_true", help="only report how many images are cached / pending")
    p.set_defaults(func=images.cmd_prefetch_assets)

    p = sub.add_parser("import-assets", help="add statement images you saved in your own browser")
    p.add_argument("folder", nargs="?", help="folder holding the saved images (matched by file name)")
    p.add_argument("--html", metavar="FILE", help="write a page linking every image still needed")
    p.set_defaults(func=images.cmd_import_assets)

    p = sub.add_parser("upload", help="upload rendered images that are not in object storage yet")
    p.add_argument("--force", action="store_true", help="re-upload everything")
    p.set_defaults(func=images.cmd_upload)

    p = sub.add_parser("cleanup", help="delete stale render files and records")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--remote", action="store_true", help="also delete stale objects from object storage")
    p.set_defaults(func=images.cmd_cleanup)

    p = sub.add_parser("stats", help="problem counts per contest")
    p.set_defaults(func=database.cmd_stats)

    p = sub.add_parser("cache-put", help="seed the HTTP cache with a page you saved yourself")
    p.add_argument("url")
    p.add_argument("file")
    p.add_argument("--content-type", default="text/html; charset=utf-8")
    p.set_defaults(func=ingest.cmd_cache_put)

    p = sub.add_parser("install-render-assets", help="download the pinned MathJax / font files")
    p.set_defaults(func=images.cmd_install_render_assets)

    p = sub.add_parser("import-legacy", help="import problems from a pre-2.0 math_problems table")
    p.add_argument("database", nargs="?", help="old database file (default: DATABASE_PATH)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--include-disabled", action="store_true", help="also import contests that are disabled")
    p.set_defaults(func=ingest.cmd_import_legacy)

    p = sub.add_parser("publish-db", help="upload a consistent database snapshot for the bot host")
    p.add_argument("--dry-run", action="store_true", help="create and check the snapshot but do not upload")
    p.set_defaults(func=database.cmd_publish_db)

    p = sub.add_parser("pull-db", help="install the newest published database snapshot")
    p.add_argument("source", nargs="?", help="pointer URL or path (default: DATABASE_SNAPSHOT_URL)")
    p.set_defaults(func=database.cmd_pull_db)

    p = sub.add_parser("migrate", help="create / upgrade the database schema")
    p.set_defaults(func=database.cmd_migrate)

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
