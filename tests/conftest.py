from __future__ import annotations

import io
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from mathbot.contests import ContestRegistry
from mathbot.db import Repository, open_database
from mathbot.http import HttpStatusError, Response, cache_key

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture
def registry() -> ContestRegistry:
    return ContestRegistry()


@pytest.fixture
def repo(tmp_path, registry) -> Repository:
    repository = Repository(open_database(tmp_path / "db" / "problems.db"))
    repository.sync_registry(registry)
    yield repository
    repository.conn.close()


class FixtureClient:
    """Stands in for PoliteClient: answers from a routing function, never the network."""

    def __init__(self, route: Callable[[str, Mapping[str, object]], str | bytes | Exception | None]) -> None:
        self.route = route
        self.calls: list[str] = []

    def get(self, url: str, params: Mapping[str, object] | None = None, **kwargs) -> Response:
        key = cache_key(url, params)
        self.calls.append(key)
        result = self.route(url, dict(params or {}))
        if result is None:
            raise HttpStatusError("HTTP 404", url=key, status=404)
        if isinstance(result, Exception):
            raise result
        content = result.encode("utf-8") if isinstance(result, str) else result
        return Response(key, 200, {"content-type": "text/html; charset=utf-8"}, content)


def make_png(width: int = 400, height: int = 200) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()
