"""Image delivery for the bot: local render first, then object storage over HTTP.

Lookup order for a problem's picture:

1. the local file recorded in the render manifest (development, or a host that
   renders on the same disk);
2. ``IMAGE_BASE_URL/<object key>`` for the current render (object storage,
   when the bot runs on another machine than the renderer).

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
from mathbot.db.repo import ProblemRow
from mathbot.render import PNG_SIGNATURE

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # stays under Discord's default upload limit
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)
CACHE_ENTRIES = 64


class ImageFetchError(Exception):
    """A remote image could not be retrieved or failed validation."""


def validate_image_response(status: int, content_type: str, data: bytes) -> None:
    """Raise :class:`ImageFetchError` unless the response is a PNG small enough for Discord."""
    if status != 200:
        raise ImageFetchError(f"HTTP {status}")
    if not content_type.lower().startswith("image/"):
        raise ImageFetchError(f"unexpected content type {content_type or 'none'!r}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageFetchError(f"image too large ({len(data)} bytes)")
    if not data.startswith(PNG_SIGNATURE):
        raise ImageFetchError("body is not a PNG")


class ImageService:
    """Finds the current picture of a problem, with a small in-memory LRU cache."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
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

    async def _fetch(self, url: str) -> bytes:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=FETCH_TIMEOUT)
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

        base_url = self.settings.storage.public_base_url
        if not base_url or render is None or not render["object_key"]:
            return None
        url = f"{base_url}/{self.settings.storage.object_key(render['object_key'])}"
        try:
            return self._remember(cache_key, await self._fetch(url))
        except ImageFetchError as exc:
            log.warning("image fetch failed", extra={"problem": problem.id, "url": url, "error": str(exc)})
            return None


def _read_png(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if data.startswith(PNG_SIGNATURE) and len(data) <= MAX_IMAGE_BYTES else None
