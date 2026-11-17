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

from pathlib import Path
from typing import Protocol

from mathbot.config import Settings, StorageSettings
from mathbot.http import atomic_write_bytes


class StorageError(Exception):
    """Upload / download failed."""


class Storage(Protocol):
    name: str

    def put(self, key: str, data: bytes, content_type: str, *, cache_control: str | None = None) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise StorageError(f"key escapes the storage root: {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str, *, cache_control: str | None = None) -> None:
        atomic_write_bytes(self._path(key), data)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError(f"no such object: {key}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    name = "s3"

    def __init__(self, settings: StorageSettings, client: object | None = None) -> None:
        if not settings.bucket:
            raise StorageError("S3_BUCKET is not configured")
        self.bucket = settings.bucket
        self.prefix = f"{settings.key_prefix}/" if settings.key_prefix else ""
        if client is None:
            import boto3  # imported lazily: only needed when S3 storage is selected

            client = boto3.client(
                "s3",
                endpoint_url=settings.endpoint_url,
                region_name=settings.region,
                aws_access_key_id=settings.access_key_id,
                aws_secret_access_key=settings.secret_access_key,
            )
        self._client = client

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, data: bytes, content_type: str, *, cache_control: str | None = None) -> None:
        try:
            self._client.put_object(  # type: ignore[attr-defined]
                Bucket=self.bucket,
                Key=self._key(key),
                Body=data,
                ContentType=content_type,
                # Keys embed the content hash, so objects never change once written.
                # Render and snapshot keys embed a content hash, so those objects never change.
                CacheControl=cache_control or "public, max-age=31536000, immutable",
            )
        except Exception as exc:  # boto3 raises many unrelated types; re-raise as one, with context
            raise StorageError(f"upload of {key} failed: {exc}") from exc

    def get(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()  # type: ignore[attr-defined]
        except Exception as exc:
            raise StorageError(f"download of {key} failed: {exc}") from exc

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))  # type: ignore[attr-defined]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise StorageError(f"head of {key} failed: {exc}") from exc
        return True

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._key(key))  # type: ignore[attr-defined]
        except Exception as exc:
            raise StorageError(f"delete of {key} failed: {exc}") from exc


def build_storage(settings: Settings) -> Storage:
    if settings.storage.backend == "s3":
        return S3Storage(settings.storage)
    # Only database snapshots (publish-db) land here; renders are served from RENDERS_DIR.
    return LocalStorage(settings.data_dir / "object_store")
