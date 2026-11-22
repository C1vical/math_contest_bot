"""Helpers shared by the command modules: settings-derived objects and argument parsing."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from mathbot.config import Settings
from mathbot.contests import ContestRegistry, ContestSeries
from mathbot.db import Repository, open_database
from mathbot.http import PoliteClient

if TYPE_CHECKING:
    from mathbot.render.assets import AssetCache
    from mathbot.render.pipeline import RenderPipeline


def registry(settings: Settings) -> ContestRegistry:
    """The contest registry with ENABLE_CONTESTS / DISABLE_CONTESTS applied."""
    return ContestRegistry(enable=settings.enable_contests, disable=settings.disable_contests)


def repo(settings: Settings, contests: ContestRegistry) -> Repository:
    """Open (creating / migrating) the database and mirror the contest registry into it."""
    repository = Repository(open_database(settings.database_path))
    repository.sync_registry(contests)
    return repository


def client(settings: Settings) -> PoliteClient:
    """Client for pages: paced by HTTP_MIN_INTERVAL, cached in HTTP_CACHE_DIR."""
    return PoliteClient(settings.http, settings.http_cache_dir)


def asset_client(settings: Settings) -> PoliteClient:
    """Client for statement images: paced by ASSET_MIN_INTERVAL; images are cached by the asset cache."""
    return PoliteClient(replace(settings.http, min_interval=settings.http.asset_min_interval))


def select(registry: ContestRegistry, names: Sequence[str]) -> list[ContestSeries]:
    """The series named by CONTEST arguments (series, families or sources); all enabled ones if none."""
    if not names:
        return registry.all()
    selected: dict[str, ContestSeries] = {}
    for name in names:
        for series in registry.select(name):
            selected[series.key] = series
    return list(selected.values())


def series_keys(registry: ContestRegistry, names: Sequence[str]) -> list[str] | None:
    """Keys of the series named by CONTEST arguments, or None (no filter) when there are none."""
    return [s.key for s in select(registry, names)] if names else None


def int_ranges(value: str) -> frozenset[int]:
    """One ``--year`` / ``--number`` token: ``2019``, ``2021-2023`` or ``1,3,5-7``.

    Used as an argparse ``type=``, so a malformed value is reported as a usage error
    before anything runs.
    """
    out: set[int] = set()
    for part in filter(None, (p.strip() for p in value.split(","))):
        low, sep, high = part.partition("-")
        try:
            first = int(low)
            last = int(high) if sep else first
        except ValueError:
            raise argparse.ArgumentTypeError(f"{part!r} is not a number or a range like 2019-2021") from None
        if last < first:
            raise argparse.ArgumentTypeError(f"{part!r} is a range that goes backwards")
        out.update(range(first, last + 1))
    if not out:
        raise argparse.ArgumentTypeError(f"{value!r} contains no numbers")
    return frozenset(out)


def int_set(values: Sequence[frozenset[int]] | None) -> frozenset[int] | None:
    """Merge the parsed ``--year`` / ``--number`` tokens; None (no filter) when there are none."""
    if not values:
        return None
    return frozenset().union(*values)


def say(message: str) -> None:
    """Progress output: printed and flushed at once, so long runs show where they are."""
    print(message, flush=True)


def asset_cache(settings: Settings, repo: Repository) -> AssetCache:
    """The statement-image cache in ASSETS_DIR, downloading through :func:`asset_client`."""
    from mathbot.render.assets import AssetCache  # the render stack is only imported by commands that use it

    return AssetCache(repo, asset_client(settings), settings.assets_dir, settings.render.asset_ttl)


def render_pipeline(settings: Settings, repo: Repository) -> RenderPipeline:
    from mathbot.render.pipeline import RenderPipeline

    return RenderPipeline(repo, settings.render, settings.renders_dir, asset_cache(settings, repo), progress=say)
