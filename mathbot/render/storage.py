"""Object storage adapters for rendered images and database snapshots.

``LocalStorage`` keeps objects in a directory (development default). With local
storage, rendered images are not copied anywhere: the bot serves them straight
from ``RENDERS_DIR`` and ``upload`` only marks them as published. Only
``publish-db`` snapshots are written to the local store.
``S3Storage`` talks to any S3-compatible service; Cloudflare R2 works by
setting ``S3_ENDPOINT_URL`` to the account endpoint. Bucket names, endpoints
and credentials come only from configuration.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol

from mathbot.config import Settings, StorageSettings
from mathbot.http import atomic_write_bytes


class StorageError(Exception):
    """Upload / download failed."""


class Storage(Protocol):
    """Where rendered images and database snapshots are published."""

    name: str

    def put(self, key: str, data: bytes, content_type: str, *, cache_control: str | None = None) -> None: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        # Checked on the key itself, not with Path.resolve(): on Windows resolve() can
        # return a "\\?\C:\..." path while another thread creates the same directory,
        # which made parallel uploads report valid keys as escaping the root.
        parts = PurePosixPath(key.replace("\\", "/")).parts
        if not parts or key.startswith(("/", "\\")) or ":" in key or any(p in {"..", "."} for p in parts):
            raise StorageError(f"key escapes the storage root: {key!r}")
        return self.root.joinpath(*parts)

    def put(self, key: str, data: bytes, content_type: str, *, cache_control: str | None = None) -> None:
        atomic_write_bytes(self._path(key), data)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    name = "s3"

    def __init__(self, settings: StorageSettings, client: object | None = None) -> None:
        if not settings.bucket:
            raise StorageError("S3_BUCKET is not configured")
        self.bucket = settings.bucket
        self.settings = settings
        if client is None:
            import boto3  # imported lazily: only needed when S3 storage is selected
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=settings.endpoint_url,
                region_name=settings.region,
                aws_access_key_id=settings.access_key_id,
                aws_secret_access_key=settings.secret_access_key,
                # One connection per parallel upload (the default pool holds 10), and
                # botocore's standard retries for throttling / transient errors.
                config=Config(max_pool_connections=32, retries={"max_attempts": 5, "mode": "standard"}),
            )
        self._client = client

    def put(self, key: str, data: bytes, content_type: str, *, cache_control: str | None = None) -> None:
        try:
            self._client.put_object(  # type: ignore[attr-defined]
                Bucket=self.bucket,
                Key=self.settings.object_key(key),
                Body=data,
                ContentType=content_type,
                # Render and snapshot keys embed a content hash, so those objects never change.
                CacheControl=cache_control or "public, max-age=31536000, immutable",
            )
        except Exception as exc:  # boto3 raises many unrelated types; re-raise as one, with context
            raise StorageError(f"upload of {key} failed: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self.settings.object_key(key))  # type: ignore[attr-defined]
        except Exception as exc:  # see put()
            raise StorageError(f"delete of {key} failed: {exc}") from exc


def build_storage(settings: Settings) -> Storage:
    """The configured publishing target: S3 / R2, or ``DATA_DIR/object_store``."""
    if settings.storage.backend == "s3":
        return S3Storage(settings.storage)
    # Only database snapshots (publish-db) land here; renders are served from RENDERS_DIR.
    return LocalStorage(settings.data_dir / "object_store")
