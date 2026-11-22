"""Polite shared HTTP client for all scrapers.

One client gives every source adapter the same behaviour:

* an honest, configurable ``User-Agent`` (no browser impersonation);
* ``robots.txt`` enforcement, including ``*`` and ``$`` path patterns;
* per-host rate limiting;
* retries with exponential backoff + jitter for 429/5xx and network errors,
  honouring ``Retry-After``;
* an on-disk cache with TTL and conditional revalidation (ETag / Last-Modified);
* detection of anti-bot challenges, which are reported and **never bypassed**.

Failures raise typed exceptions; nothing here returns an empty body in place
of an error.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests

from mathbot.config import HttpSettings

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Network-level failures worth retrying; any other requests exception fails at once.
RETRY_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)
MAX_RETRY_AFTER = 300.0


class HttpError(Exception):
    """Base class for all fetch failures."""

    def __init__(self, message: str, *, url: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class HttpStatusError(HttpError):
    """The server answered with an unexpected status code."""


class TransportError(HttpError):
    """Connection failure or timeout after all retries."""


class SourceBlockedError(HttpError):
    """The source presented an anti-bot challenge. We stop; we do not work around it."""


class RobotsDisallowedError(HttpError):
    """robots.txt disallows this URL for our user agent."""


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: Mapping[str, str]
    content: bytes
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode(_charset(self.headers.get("content-type", "")), errors="replace")

    def json(self) -> object:
        try:
            return json.loads(self.content)
        except ValueError as exc:
            raise HttpError(f"response is not valid JSON: {exc}", url=self.url, status=self.status) from exc

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _charset(content_type: str) -> str:
    """The declared charset if Python knows it, else UTF-8 (``charset=utf8mb4`` is seen in the wild)."""
    for part in content_type.split(";"):
        part = part.strip().lower()
        if part.startswith("charset="):
            label = part.split("=", 1)[1].strip("\"'")
            try:
                return codecs.lookup(label).name
            except LookupError:
                return "utf-8"
    return "utf-8"


# --------------------------------------------------------------------------- robots.txt


@dataclass
class RobotsRules:
    """Minimal robots.txt matcher with Google-style ``*`` / ``$`` and longest-match-wins."""

    groups: dict[str, list[tuple[bool, str]]] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> RobotsRules:
        groups: dict[str, list[tuple[bool, str]]] = {}
        agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            name, value = (part.strip() for part in line.split(":", 1))
            name = name.lower()
            if name == "user-agent":
                if in_rules:
                    agents, in_rules = [], False
                agents.append(value.lower())
                groups.setdefault(value.lower(), [])
            elif name in {"allow", "disallow"}:
                in_rules = True
                if value:  # an empty Disallow means "allow everything"
                    for agent in agents:
                        groups[agent].append((name == "allow", value))
        return cls(groups)

    def _rules_for(self, user_agent: str) -> list[tuple[bool, str]]:
        token = user_agent.split("/", 1)[0].strip().lower()
        matches = [agent for agent in self.groups if agent != "*" and agent in token]
        if matches:
            return self.groups[max(matches, key=len)]
        return self.groups.get("*", [])

    def allowed(self, user_agent: str, path: str) -> bool:
        best_len, best_allow = -1, True
        for allow, pattern in self._rules_for(user_agent):
            if _robots_match(pattern, path):
                size = len(pattern)
                if size > best_len or (size == best_len and allow):
                    best_len, best_allow = size, allow
        return best_allow


def _robots_match(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    parts = pattern.split("*")
    if not path.startswith(parts[0]):
        return False
    pos = len(parts[0])
    for part in parts[1:]:
        found = path.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    if anchored:
        return pos == len(path) or (len(parts) > 1 and path.endswith(parts[-1]))
    return True


# --------------------------------------------------------------------------- cache


class DiskCache:
    """Content cache keyed by request URL. Bodies and metadata are written atomically."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _paths(self, key: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        base = self.root / digest[:2] / digest
        return base.with_suffix(".body"), base.with_suffix(".json")

    def load(self, key: str) -> tuple[dict, bytes] | None:
        body_path, meta_path = self._paths(key)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta, body_path.read_bytes()
        except (FileNotFoundError, ValueError):
            return None

    def store(self, key: str, *, url: str, status: int, headers: Mapping[str, str], content: bytes) -> None:
        body_path, meta_path = self._paths(key)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"url": url, "status": status, "headers": dict(headers), "fetched_at": time.time()}
        atomic_write_bytes(body_path, content)
        atomic_write_bytes(meta_path, json.dumps(meta).encode("utf-8"))

    def touch(self, key: str) -> None:
        _, meta_path = self._paths(key)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return
        meta["fetched_at"] = time.time()
        atomic_write_bytes(meta_path, json.dumps(meta).encode("utf-8"))


REPLACE_ATTEMPTS = 6
REPLACE_RETRY_DELAY = 0.1  # seconds, growing with each attempt (1.5 s in total)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file + rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        _replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _replace(source: Path, target: Path) -> None:
    """``os.replace``, retried briefly on Windows "Access is denied".

    Antivirus scanners and the search indexer open newly written files for a moment,
    and Windows refuses to rename a file another process has open.
    """
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                raise
            time.sleep(REPLACE_RETRY_DELAY * attempt)


# --------------------------------------------------------------------------- client


def is_challenge(status: int, headers: Mapping[str, str], body: bytes) -> bool:
    """True for anti-bot interstitials (observed: Cloudflare ``cf-mitigated: challenge``)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if lowered.get("cf-mitigated", "").lower() == "challenge":
        return True
    if status in {403, 429, 503} and "text/html" in lowered.get("content-type", ""):
        head = body[:4096].lower()
        return b"just a moment" in head or b"challenges.cloudflare.com" in head or b"captcha" in head
    return False


def _with_query(url: str, params: Mapping[str, object] | None) -> str:
    """``url`` with ``params`` appended in the order given, as the request will send them."""
    if not params:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode([(k, str(v)) for k, v in params.items()])}"


def cache_key(url: str, params: Mapping[str, object] | None) -> str:
    """The URL with its query parameters in a stable order: the key for caching and logging."""
    if not params:
        return url
    return f"{url}?{urlencode(sorted((k, str(v)) for k, v in params.items()))}"


class PoliteClient:
    """Rate-limited, caching, retrying GET client. Thread-safe."""

    def __init__(
        self,
        settings: HttpSettings,
        cache_dir: Path | None = None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.cache = DiskCache(cache_dir) if cache_dir else None
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._next_slot: dict[str, float] = {}
        self._robots: dict[str, RobotsRules | None] = {}
        self._robots_lock = threading.Lock()
        self._override_warned: set[str] = set()

    # -- public API

    def get(
        self,
        url: str,
        params: Mapping[str, object] | None = None,
        *,
        refresh: bool = False,
        cache_ttl: float | None = None,
        use_cache: bool = True,
    ) -> Response:
        """GET ``url``. ``refresh=True`` ignores cache freshness (still revalidates)."""
        key = cache_key(url, params)
        ttl = self.settings.cache_ttl if cache_ttl is None else cache_ttl
        cached = self.cache.load(key) if (self.cache and use_cache) else None

        if cached and not refresh and time.time() - cached[0].get("fetched_at", 0) < ttl:
            meta, body = cached
            return Response(meta["url"], meta["status"], meta["headers"], body, from_cache=True)

        self._check_robots(_with_query(url, params))

        conditional: dict[str, str] = {}
        if cached:
            headers = {k.lower(): v for k, v in cached[0]["headers"].items()}
            if headers.get("etag"):
                conditional["If-None-Match"] = headers["etag"]
            if headers.get("last-modified"):
                conditional["If-Modified-Since"] = headers["last-modified"]

        response = self._request(url, params, conditional)
        if response.status == 304 and cached:
            self.cache.touch(key)  # type: ignore[union-attr]
            meta, body = cached
            return Response(meta["url"], meta["status"], meta["headers"], body, from_cache=True)

        if self.cache and use_cache:
            self.cache.store(
                key, url=response.url, status=response.status, headers=response.headers, content=response.content
            )
        return response

    def seed_cache(self, url: str, content: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        """Store a manually obtained response (e.g. a page saved from your own browser)."""
        if not self.cache:
            raise RuntimeError("no cache directory configured")
        self.cache.store(url, url=url, status=200, headers={"content-type": content_type}, content=content)

    # -- internals

    def _throttle(self, host: str) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + self.settings.min_interval
        if slot > now:
            self._sleep(slot - now)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            delay = _parse_retry_after(retry_after)
            if delay is not None:
                return min(delay, MAX_RETRY_AFTER) + self._rng.uniform(0.0, 0.5)
        delay = min(self.settings.backoff_cap, self.settings.backoff_base * (2 ** (attempt - 1)))
        return delay + self._rng.uniform(0.0, delay / 2 if delay else 0.0)

    def _request(self, url: str, params: Mapping[str, object] | None, extra_headers: Mapping[str, str]) -> Response:
        host = urlsplit(url).netloc
        headers = {"User-Agent": self.settings.user_agent, "Accept-Encoding": "gzip, deflate", **extra_headers}
        attempts = self.settings.max_retries + 1
        last_error: HttpError = TransportError("no request was made", url=url)  # replaced by the first attempt

        for attempt in range(1, attempts + 1):
            self._throttle(host)
            try:
                raw = self._session.get(url, params=params, headers=headers, timeout=self.settings.timeout)
            except RETRY_EXCEPTIONS as exc:
                last_error = TransportError(f"{type(exc).__name__}: {exc}", url=url)
                retry_after = None
            except requests.RequestException as exc:  # e.g. an invalid URL: retrying cannot help
                raise TransportError(f"{type(exc).__name__}: {exc}", url=url) from exc
            else:
                status, content = raw.status_code, raw.content
                if is_challenge(status, raw.headers, content):
                    raise SourceBlockedError(
                        f"{host} answered with an anti-bot challenge (HTTP {status}); not bypassing it",
                        url=url,
                        status=status,
                    )
                if status not in RETRY_STATUSES:
                    if status >= 400:
                        raise HttpStatusError(f"HTTP {status}", url=url, status=status)
                    return Response(raw.url, status, dict(raw.headers), content)
                last_error = HttpStatusError(f"HTTP {status}", url=url, status=status)
                retry_after = raw.headers.get("Retry-After")

            if attempt < attempts:
                delay = self._backoff(attempt, retry_after)
                log.warning(
                    "request failed, retrying",
                    extra={"url": url, "attempt": attempt, "delay": round(delay, 2), "error": str(last_error)},
                )
                self._sleep(delay)

        raise last_error

    def _check_robots(self, url: str) -> None:
        if not self.settings.respect_robots:
            return
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        host = (parts.hostname or "").lower()
        if host in self.settings.robots_override_hosts:
            if host not in self._override_warned:
                self._override_warned.add(host)
                log.warning(
                    "ignoring robots.txt for this host because ROBOTS_OVERRIDE_HOSTS lists it",
                    extra={"host": host},
                )
            return
        with self._robots_lock:
            if origin not in self._robots:
                self._robots[origin] = self._load_robots(origin)
            rules = self._robots[origin]
        if rules is None:
            return
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        if not rules.allowed(self.settings.user_agent, path):
            raise RobotsDisallowedError(f"robots.txt at {origin} disallows {path}", url=url)

    def _load_robots(self, origin: str) -> RobotsRules | None:
        """Fetch robots.txt. 4xx means "no rules"; other failures are fatal for the host."""
        robots_url = f"{origin}/robots.txt"
        try:
            response = self._request(robots_url, None, {})
        except HttpStatusError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                return None
            raise
        return RobotsRules.parse(response.text)


def _parse_retry_after(value: str) -> float | None:
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())
