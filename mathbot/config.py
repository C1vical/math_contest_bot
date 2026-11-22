"""Typed runtime configuration.

Everything external (paths, tokens, rate limits, storage) is read from the
environment exactly once, in :func:`load_settings`. Importing this module has
no side effects; nothing here reads the environment at import time.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_USER_AGENT = "math-contest-bot/2.0 (educational Discord bot; set HTTP_USER_AGENT with contact info)"


class ConfigError(ValueError):
    """Raised when the environment holds a missing or malformed setting."""


@dataclass(frozen=True)
class HttpSettings:
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    min_interval: float = 1.0  # seconds between requests to one host
    asset_min_interval: float = 1.0  # the same, for statement images (prefetch-assets / render)
    cache_ttl: float = 24 * 3600.0
    respect_robots: bool = True
    # Hosts whose robots.txt is deliberately ignored (a user decision, see README).
    # Never affects anti-bot challenge detection.
    robots_override_hosts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StorageSettings:
    backend: str = "local"  # "local" or "s3" (S3-compatible, e.g. Cloudflare R2)
    bucket: str | None = None
    endpoint_url: str | None = None
    region: str = "auto"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    key_prefix: str = ""
    public_base_url: str | None = None  # where the bot downloads images from

    def object_key(self, key: str) -> str:
        """``key`` with ``S3_KEY_PREFIX`` in front: the name of the object in the bucket."""
        return f"{self.key_prefix}/{key}" if self.key_prefix else key


@dataclass(frozen=True)
class RenderSettings:
    concurrency: int = 4
    retries: int = 2
    timeout_ms: int = 30_000
    device_scale_factor: float = 2.0
    viewport_width: int = 1000
    asset_ttl: float = 30 * 24 * 3600.0


@dataclass(frozen=True)
class BotSettings:
    token: str | None = None
    allowed_channel_ids: frozenset[int] = frozenset()
    allowed_role_ids: frozenset[int] = frozenset()
    admin_role_ids: frozenset[int] = frozenset()
    admin_user_ids: frozenset[int] = frozenset()
    guild_id: int | None = None  # sync commands to one guild (instant) instead of globally
    sync_commands: bool = True
    announce_channel_id: int | None = None
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    health_enabled: bool = True
    command_cooldown: float = 3.0  # seconds between problem commands per user; 0 disables


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    renders_dir: Path
    assets_dir: Path
    render_assets_dir: Path
    http_cache_dir: Path
    database_snapshot_url: str | None = None
    log_level: str = "INFO"
    ingest_concurrency: int = 2
    enable_contests: frozenset[str] = frozenset()
    disable_contests: frozenset[str] = frozenset()
    http: HttpSettings = field(default_factory=HttpSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    render: RenderSettings = field(default_factory=RenderSettings)
    bot: BotSettings = field(default_factory=BotSettings)


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _optional_int(env: Mapping[str, str], name: str, *, minimum: int | None = None) -> int | None:
    raw = _get(env, name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int | None = None) -> int:
    value = _optional_int(env, name, minimum=minimum)
    return default if value is None else value


def _float(env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _id_set(env: Mapping[str, str], *names: str) -> frozenset[int]:
    ids: set[int] = set()
    for name in names:
        raw = _get(env, name)
        if raw is None:
            continue
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise ConfigError(f"{name} must be a comma-separated list of numeric IDs, got {part!r}")
            ids.add(int(part))
    return frozenset(ids)


def _name_set(env: Mapping[str, str], name: str) -> frozenset[str]:
    raw = _get(env, name) or ""
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


def _path(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = _get(env, name)
    if raw is None:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_settings(env: Mapping[str, str] | None = None, *, load_dotenv_file: bool = True) -> Settings:
    """Build :class:`Settings` from ``env`` (defaults to ``os.environ`` plus ``.env``)."""
    if env is None:
        if load_dotenv_file:
            from dotenv import load_dotenv

            load_dotenv(PROJECT_ROOT / ".env")
        env = os.environ

    data_dir = _path(env, "DATA_DIR", PROJECT_ROOT / "data")

    backend = (_get(env, "STORAGE_BACKEND") or "local").lower()
    if backend not in {"local", "s3"}:
        raise ConfigError(f"STORAGE_BACKEND must be 'local' or 's3', got {backend!r}")
    storage = StorageSettings(
        backend=backend,
        bucket=_get(env, "S3_BUCKET"),
        endpoint_url=_get(env, "S3_ENDPOINT_URL"),
        region=_get(env, "S3_REGION") or "auto",
        access_key_id=_get(env, "S3_ACCESS_KEY_ID"),
        secret_access_key=_get(env, "S3_SECRET_ACCESS_KEY"),
        key_prefix=(_get(env, "S3_KEY_PREFIX") or "").strip("/"),
        public_base_url=(_get(env, "IMAGE_BASE_URL") or "").rstrip("/") or None,
    )
    if backend == "s3":
        missing = [
            name
            for name, value in (
                ("S3_BUCKET", storage.bucket),
                ("S3_ACCESS_KEY_ID", storage.access_key_id),
                ("S3_SECRET_ACCESS_KEY", storage.secret_access_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"STORAGE_BACKEND=s3 requires {', '.join(missing)}")

    http = HttpSettings(
        user_agent=_get(env, "HTTP_USER_AGENT") or DEFAULT_USER_AGENT,
        timeout=_float(env, "HTTP_TIMEOUT", 30.0, minimum=1.0),
        max_retries=_int(env, "HTTP_MAX_RETRIES", 4, minimum=0),
        backoff_base=_float(env, "HTTP_BACKOFF_BASE", 1.0),
        backoff_cap=_float(env, "HTTP_BACKOFF_CAP", 60.0),
        min_interval=_float(env, "HTTP_MIN_INTERVAL", 1.0),
        asset_min_interval=_float(env, "ASSET_MIN_INTERVAL", 1.0),
        cache_ttl=_float(env, "HTTP_CACHE_TTL", 24 * 3600.0),
        respect_robots=_bool(env, "HTTP_RESPECT_ROBOTS", True),
        robots_override_hosts=frozenset(
            h.strip().lower() for h in (_get(env, "ROBOTS_OVERRIDE_HOSTS") or "").split(",") if h.strip()
        ),
    )

    render = RenderSettings(
        concurrency=_int(env, "RENDER_CONCURRENCY", 4, minimum=1),
        retries=_int(env, "RENDER_RETRIES", 2, minimum=0),
        timeout_ms=_int(env, "RENDER_TIMEOUT_MS", 30_000, minimum=1000),
        device_scale_factor=_float(env, "RENDER_SCALE", 2.0, minimum=0.5),
        viewport_width=_int(env, "RENDER_WIDTH", 1000, minimum=320),
        asset_ttl=_float(env, "ASSET_TTL", 30 * 24 * 3600.0),
    )

    bot = BotSettings(
        token=_get(env, "DISCORD_TOKEN"),
        allowed_channel_ids=_id_set(env, "ALLOWED_CHANNEL_IDS"),
        allowed_role_ids=_id_set(env, "ALLOWED_ROLE_IDS"),
        admin_role_ids=_id_set(env, "ADMIN_ROLE_IDS"),
        admin_user_ids=_id_set(env, "ADMIN_USER_IDS"),
        guild_id=_optional_int(env, "GUILD_ID"),
        sync_commands=_bool(env, "SYNC_COMMANDS", True),
        announce_channel_id=_optional_int(env, "ANNOUNCE_CHANNEL_ID"),
        health_host=_get(env, "HEALTH_HOST") or "0.0.0.0",
        health_port=_int(env, "PORT", 8080, minimum=1),
        health_enabled=_bool(env, "HEALTH_ENABLED", True),
        command_cooldown=_float(env, "COMMAND_COOLDOWN_SECONDS", 3.0),
    )

    return Settings(
        data_dir=data_dir,
        database_path=_path(env, "DATABASE_PATH", data_dir / "problems.db"),
        renders_dir=_path(env, "RENDERS_DIR", data_dir / "renders"),
        assets_dir=_path(env, "ASSETS_DIR", data_dir / "assets"),
        render_assets_dir=_path(env, "RENDER_ASSETS_DIR", data_dir / "render_assets"),
        database_snapshot_url=_get(env, "DATABASE_SNAPSHOT_URL"),
        http_cache_dir=_path(env, "HTTP_CACHE_DIR", data_dir / "http_cache"),
        log_level=(_get(env, "LOG_LEVEL") or "INFO").upper(),
        ingest_concurrency=_int(env, "INGEST_CONCURRENCY", 2, minimum=1),
        enable_contests=_name_set(env, "ENABLE_CONTESTS"),
        disable_contests=_name_set(env, "DISABLE_CONTESTS"),
        http=http,
        storage=storage,
        render=render,
        bot=bot,
    )


def require_bot_token(settings: Settings) -> str:
    """Return the Discord token or raise a clear error naming the variable."""
    if not settings.bot.token:
        raise ConfigError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    return settings.bot.token
