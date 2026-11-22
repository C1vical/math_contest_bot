"""prefetch-assets, import-assets, render, upload, cleanup, install-render-assets: turning problems into images."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlsplit

from mathbot.cli import common
from mathbot.config import Settings
from mathbot.db import Repository
from mathbot.htmlnorm import remote_images

PROGRESS_EVERY_IMAGES = 100


def _statement_images(repo: Repository, series_keys: Sequence[str] | None = None) -> list[str]:
    """Distinct remote images used by the stored statements (of ``series_keys``, or all), in order."""
    return list(dict.fromkeys(url for p in repo.iter_problems(series_keys) for url in remote_images(p.statement_html)))


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def cmd_prefetch_assets(args: argparse.Namespace, settings: Settings) -> int:
    """Download statement images ahead of rendering, in resumable batches."""
    from mathbot.render.assets import AssetError

    registry = common.registry(settings)
    repo = common.repo(settings, registry)
    cache = common.asset_cache(settings, repo)

    wanted = _statement_images(repo, common.series_keys(registry, args.contests))
    states = {url: cache.state(url) for url in wanted}
    pending = [url for url in wanted if states[url] == "missing"]
    failed = sum(1 for state in states.values() if state == "failed")
    interval = settings.http.asset_min_interval
    print(
        f"{len(wanted):,} distinct image(s): {len(wanted) - len(pending) - failed:,} cached, "
        f"{failed:,} failed recently (retried later), {len(pending):,} to fetch "
        f"(~{len(pending) * interval / 3600:.1f} h at one request per {interval:g} s, ASSET_MIN_INTERVAL)"
    )
    blocked = sorted({_host(u) for u in pending} - settings.http.robots_override_hosts)
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
            if index % PROGRESS_EVERY_IMAGES == 0:
                print(f"  {index:,}/{len(batch):,}  ({fetched:,} cached, {errors:,} failed)", flush=True)
    except KeyboardInterrupt:
        print(f"interrupted: {fetched:,} cached, {errors:,} failed; run again to continue", file=sys.stderr)
        return 130
    print(f"done: {fetched:,} cached, {errors:,} failed, {len(pending) - len(batch):,} left for later runs")
    return 0


def cmd_import_assets(args: argparse.Namespace, settings: Settings) -> int:
    """Put images the user saved in their own browser into the asset cache, matched by file name."""
    from mathbot.render.assets import AssetError

    repo = common.repo(settings, common.registry(settings))
    cache = common.asset_cache(settings, repo)
    # Images problems need that aren't cached and that the pipeline may not download:
    # hosts behind an anti-bot challenge or a robots.txt it respects (e.g. AoPS wiki files).
    wanted = [
        url
        for url in _statement_images(repo)
        if _host(url) not in settings.http.robots_override_hosts and cache.state(url) != "cached"
    ]
    name = {url: unquote(urlsplit(url).path.rsplit("/", 1)[-1]) for url in wanted}
    print(f"{len(wanted)} image(s) the pipeline can't download and you haven't added yet")

    if args.html:
        links = "\n".join(f'<li><a href="{escape(u)}" target="_blank">{escape(name[u])}</a></li>' for u in wanted)
        Path(args.html).write_text(
            "<!doctype html><meta charset='utf-8'><title>Images to save</title>"
            "<p>Open each link and save the image (keep its file name) into one folder, then run "
            f"<code>python -m mathbot import-assets FOLDER</code>.</p>\n<ol>\n{links}\n</ol>\n",
            encoding="utf-8",
        )
        print(f"links written to {args.html}")
    if not args.folder:
        if not args.html:
            for url in wanted:
                print(f"  {name[url]}  {url}")
        return 0

    files = {p.name.lower(): p for p in Path(args.folder).rglob("*") if p.is_file()}
    imported = invalid = 0
    for url in wanted:
        path = files.get(name[url].lower())
        if path is None:
            continue
        try:
            cache.store(url, path.read_bytes(), expires=False)
            imported += 1
        except AssetError as exc:
            invalid += 1
            print(f"  not an image, skipped: {path} ({exc})")
    print(f"imported {imported}, skipped {invalid}, still missing {len(wanted) - imported - invalid}")
    if imported:
        print("run 'python -m mathbot render' to render the problems that use them")
    return 0 if not invalid else 1


def cmd_render(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.pipeline import PlaywrightRenderer

    registry = common.registry(settings)
    repo = common.repo(settings, registry)
    pipeline = common.render_pipeline(settings, repo)
    problems = pipeline.plan(common.series_keys(registry, args.contests), limit=args.limit)
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

    repo = common.repo(settings, common.registry(settings))
    pipeline = common.render_pipeline(settings, repo)
    if settings.storage.backend == "local":
        # The bot serves RENDERS_DIR directly; a second copy would only duplicate it.
        report = pipeline.upload(None, force=args.force)
        print(f"storage backend: local (images are served from {settings.renders_dir}; nothing is copied)")
    else:
        storage = build_storage(settings)
        report = pipeline.upload(storage, force=args.force)
        print(f"storage backend: {storage.name}")
    print(report.format())
    return 0 if report.failed == 0 else 1


def cmd_cleanup(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.storage import StorageError, build_storage

    repo = common.repo(settings, common.registry(settings))
    try:
        # Local renders have no remote copy: deleting the local file is all there is to do.
        storage = build_storage(settings) if args.remote and settings.storage.backend != "local" else None
        counts = common.render_pipeline(settings, repo).cleanup(storage, dry_run=args.dry_run)
    except StorageError as exc:  # records already cleaned stay cleaned; run again to finish
        print(f"error: {exc}", file=sys.stderr)
        return 1
    prefix = "would remove" if args.dry_run else "removed"
    print(f"{prefix}: " + ", ".join(f"{value} {name.replace('_', ' ')}" for name, value in counts.items()))
    return 0


def cmd_install_render_assets(args: argparse.Namespace, settings: Settings) -> int:
    from mathbot.render.vendor import RenderAssetsMissing, install_assets

    try:
        outcomes = install_assets(settings.render_assets_dir, common.client(settings))
    except RenderAssetsMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path, outcome in outcomes.items():
        print(f"{outcome:>9}  {path}")
    print(f"render assets ready in {settings.render_assets_dir}")
    return 0
