"""Download-once cache for remote images referenced by problem statements.

Before a problem is rendered, each remote ``<img>`` is fetched through the
polite HTTP client, validated, stored under its SHA-256 and recorded in the
``assets`` table. The render HTML then embeds the cached bytes as ``data:``
URIs, so screenshots never depend on a third-party host being up and a missing
image fails the render instead of producing a broken picture.

Failures are remembered so a render run doesn't hit the same URL again: a
permanent one (404, anti-bot challenge, not an image) for a day, a temporary one
(network error, 5xx, 429) for an hour. A robots.txt refusal is not remembered: it
costs the host nothing and is lifted as soon as ``ROBOTS_OVERRIDE_HOSTS`` lists the
host. :attr:`AssetError.retryable` is True for temporary failures and robots.txt
refusals, which may succeed later.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from mathbot.db.connection import iso_utc
from mathbot.db.repo import Repository
from mathbot.htmlnorm import is_remote_url
from mathbot.http import (
    RETRY_STATUSES,
    HttpError,
    PoliteClient,
    RobotsDisallowedError,
    SourceBlockedError,
    TransportError,
    atomic_write_bytes,
)
from mathbot.render import PNG_SIGNATURE

log = logging.getLogger(__name__)

MAX_ASSET_BYTES = 10 * 1024 * 1024
FAILURE_TTL = 24 * 3600.0  # permanently failed downloads are not retried for a day
TEMPORARY_FAILURE_TTL = 3600.0  # network errors / 5xx are retried after an hour
TEMPORARY = "temporary: "  # prefix of a stored error that may succeed on retry
_ROBOTS_REFUSAL = "robots.txt "  # stored by earlier versions; never treated as a cached failure
_SIGNATURES = (
    (PNG_SIGNATURE, "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),
)


class AssetError(Exception):
    """An image could not be fetched or is not a usable image.

    ``retryable`` is True when a later attempt may succeed: a network error, 5xx or
    429, or a robots.txt refusal (lifted by ``ROBOTS_OVERRIDE_HOSTS``).
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_temporary(exc: Exception) -> bool:
    if isinstance(exc, SourceBlockedError) or not isinstance(exc, HttpError):
        return False
    return isinstance(exc, TransportError) or exc.status in RETRY_STATUSES


def sniff_image(data: bytes, declared_type: str = "") -> tuple[str, str]:
    """Return ``(content type, extension)`` from the bytes themselves, not the header."""
    for signature, content_type, extension in _SIGNATURES:
        if data.startswith(signature):
            return content_type, extension
    head = data[:512].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:4096].lower():
        return "image/svg+xml", ".svg"
    raise AssetError(f"not a recognised image (declared content type: {declared_type or 'none'})")


def _iso_after(seconds: float) -> str:
    return iso_utc(datetime.now(timezone.utc) + timedelta(seconds=seconds))


class AssetCache:
    """Downloaded statement images, stored under ``root`` by SHA-256 and recorded in ``assets``."""

    def __init__(self, repo: Repository, client: PoliteClient, root: Path, ttl: float) -> None:
        self.repo = repo
        self.client = client
        self.root = root
        self.ttl = ttl

    def _fresh(self, row) -> Path | None:
        if row is None or row["status"] != "ok" or not row["local_path"]:
            return None
        path = self.root / row["local_path"]
        if not path.is_file():
            return None
        if row["expires_at"] and row["expires_at"] < iso_utc():
            return None
        return path

    def ensure(self, url: str) -> tuple[bytes, str]:
        """Return ``(bytes, content type)`` for ``url``, downloading only when needed."""
        row = self.repo.get_asset(url)
        path = self._fresh(row)
        if path is not None:
            return path.read_bytes(), row["content_type"]
        now = iso_utc()
        if self._recent_failure(row, now):
            # A recent failure is not retried on every render run; it is retried once the
            # negative-cache entry expires.
            raise AssetError(
                f"{url}: {row['error']} (cached failure, retried after {row['expires_at']})",
                retryable=row["error"].startswith(TEMPORARY),
            )

        try:
            response = self.client.get(url, use_cache=False)
            return self.store(url, response.content, response.headers.get("content-type", ""))
        except RobotsDisallowedError as exc:
            raise AssetError(f"{url}: {exc}", retryable=True) from exc
        except (HttpError, AssetError) as exc:
            temporary = _is_temporary(exc)
            error = f"{TEMPORARY}{exc}" if temporary else str(exc)
            delay = TEMPORARY_FAILURE_TTL if temporary else FAILURE_TTL
            self.repo.upsert_asset(url, status="failed", error=error, expires_at=_iso_after(min(self.ttl, delay)))
            raise AssetError(f"{url}: {error}", retryable=temporary) from exc

    def store(self, url: str, data: bytes, declared_type: str = "", *, expires: bool = True) -> tuple[bytes, str]:
        """Validate ``data`` as an image and cache it as the copy of ``url``. No network.

        Used for downloads, and for images the user saved in their own browser
        (``import-assets``), e.g. wiki files the pipeline can't fetch. Those are
        stored with ``expires=False``: they can't be downloaded again, so they are
        kept until replaced.
        """
        if not data:
            raise AssetError("empty response body")
        if len(data) > MAX_ASSET_BYTES:
            raise AssetError(f"image is {len(data)} bytes, over the {MAX_ASSET_BYTES} byte limit")
        content_type, extension = sniff_image(data, declared_type)
        digest = hashlib.sha256(data).hexdigest()
        relative = f"{digest[:2]}/{digest}{extension}"
        atomic_write_bytes(self.root / relative, data)
        expires_at = _iso_after(self.ttl) if expires else None
        self.repo.upsert_asset(
            url,
            status="ok",
            sha256=digest,
            content_type=content_type,
            size_bytes=len(data),
            local_path=relative,
            expires_at=expires_at,
        )
        return data, content_type

    @staticmethod
    def _recent_failure(row, now: str) -> bool:
        return (
            row is not None
            and row["status"] == "failed"
            and bool(row["expires_at"])
            and row["expires_at"] > now
            and not (row["error"] or "").startswith(_ROBOTS_REFUSAL)
        )

    def state(self, url: str) -> str:
        """``"cached"``, ``"failed"`` (recent failure, not retried yet) or ``"missing"``. No network."""
        row = self.repo.get_asset(url)
        if self._fresh(row) is not None:
            return "cached"
        return "failed" if self._recent_failure(row, iso_utc()) else "missing"

    def has(self, url: str) -> bool:
        """Whether a fresh copy of ``url`` is stored. No network, and the bytes are not read."""
        return self._fresh(self.repo.get_asset(url)) is not None

    def cached(self, url: str) -> tuple[bytes, str] | None:
        """The stored copy of ``url`` if it is present and fresh; never touches the network."""
        row = self.repo.get_asset(url)
        path = self._fresh(row)
        return (path.read_bytes(), row["content_type"]) if path is not None else None

    def inline(self, problem_id: str, html: str, *, fetch: bool = True) -> str:
        """Return ``html`` with every remote image replaced by a ``data:`` URI of the cached copy.

        With ``fetch=False`` only the local cache is used (no network), and an image
        that is not cached raises :class:`AssetError`.
        """
        soup = BeautifulSoup(html, "html.parser")
        changed = False
        for img in soup.find_all("img"):
            src = img.get("src")
            if not is_remote_url(src):
                continue
            if fetch:
                data, content_type = self.ensure(src)
            else:
                hit = self.cached(src)
                if hit is None:
                    raise AssetError(f"{src}: not in the asset cache (prefetch failed or was skipped)")
                data, content_type = hit
            row = self.repo.get_asset(src)
            if row is not None:
                self.repo.link_asset(problem_id, row["id"])
            img["src"] = f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"
            changed = True
        return str(soup) if changed else html
