"""Image delivery for the bot: local render first, then object storage over HTTP.

Lookup order for a problem's picture:

1. the local file recorded in the render manifest (development, or a host that
   renders on the same disk);
2. ``IMAGE_BASE_URL/<object key>`` for the current render;
3. ``IMAGE_BASE_URL/<year>_<contest>_<n>.png`` - the pre-2.0 key, so a bucket
   filled by the old bot keeps working (``IMAGE_LEGACY_KEYS=false`` disables it).

Remote fetches have a timeout and validate status, content type, size and the
PNG signature; anything else is treated as "no image", never sent to Discord.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from pathlib import Path

import aiohttp

from mathbot.bot.services import Database
from mathbot.config import Settings
from mathbot.contests import ContestRegistry, legacy_image_key
from mathbot.db.repo import ProblemRow
from mathbot.render.pipeline import PNG_SIGNATURE

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # stays under Discord's default upload limit
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)
CACHE_ENTRIES = 64


class ImageFetchError(Exception):
    """A remote image could not be retrieved or failed validation."""


def validate_image_response(status: int, content_type: str, data: bytes) -> None:
    if status != 200:
        raise ImageFetchError(f"HTTP {status}")
    if not content_type.lower().startswith("image/"):
        raise ImageFetchError(f"unexpected content type {content_type or 'none'!r}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageFetchError(f"image too large ({len(data)} bytes)")
    if not data.startswith(PNG_SIGNATURE):
        raise ImageFetchError("body is not a PNG")


class ImageService:
    def __init__(self, settings: Settings, db: Database, registry: ContestRegistry) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry
        self._session: aiohttp.ClientSession | None = None
        self._cache: OrderedDict[str, bytes] = OrderedDict()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _remember(self, key: str, data: bytes) -> bytes:
        self._cache[key] = data
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return data

    async def _fetch(self, key: str) -> bytes:
        base = self.settings.storage.public_base_url
        assert base is not None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=FETCH_TIMEOUT)
        url = f"{base}/{key}"
        try:
            async with self._session.get(url) as response:
                data = await response.content.read(MAX_IMAGE_BYTES + 1)
                validate_image_response(response.status, response.headers.get("Content-Type", ""), data)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ImageFetchError(f"{type(exc).__name__}: {exc}") from exc

    async def get(self, problem: ProblemRow) -> bytes | None:
        """PNG bytes for the problem's current content, or ``None`` if no valid image exists."""
        cache_key = f"{problem.id}:{problem.content_hash}"
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        render = await self.db.call(lambda repo: repo.current_render(problem.id))
        if render is not None and render["local_path"]:
            path = Path(self.settings.renders_dir) / render["local_path"]
            data = await asyncio.to_thread(_read_png, path)
            if data is not None:
                return self._remember(cache_key, data)

        if not self.settings.storage.public_base_url:
            return None

        keys: list[str] = []
        if render is not None and render["object_key"]:
            prefix = f"{self.settings.storage.key_prefix}/" if self.settings.storage.key_prefix else ""
            keys.append(f"{prefix}{render['object_key']}")
        series = self.registry.resolve(problem.series_key, include_disabled=True)
        if self.settings.storage.legacy_keys and series is not None:
            keys.append(legacy_image_key(series, problem.year, problem.number))

        for key in keys:
            try:
                return self._remember(cache_key, await self._fetch(key))
            except ImageFetchError as exc:
                log.warning("image fetch failed", extra={"problem": problem.id, "key": key, "error": str(exc)})
        return None


def _read_png(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if data.startswith(PNG_SIGNATURE) and len(data) <= MAX_IMAGE_BYTES else None
