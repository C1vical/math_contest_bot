"""Shared fixtures and fakes. Nothing here touches the network."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from mathbot.config import Settings, load_settings
from mathbot.contests import ContestRegistry
from mathbot.db import ProblemRow, Repository, open_database
from mathbot.htmlnorm import normalize_fragment
from mathbot.http import HttpStatusError, Response, cache_key
from mathbot.models import EditionRef, ParsedProblem

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


@contextmanager
def settings_repo(settings: Settings) -> Iterator[Repository]:
    """The database a CLI command run with ``settings`` will open, closed again afterwards."""
    repository = Repository(open_database(settings.database_path))
    repository.sync_registry(ContestRegistry())
    try:
        yield repository
    finally:
        repository.conn.close()


# --------------------------------------------------------------------------- stored problems


def store(repo: Repository, ref: EditionRef, problem: ParsedProblem, run_id: int | None = None) -> str:
    """Normalize and upsert one problem the way ingestion does; returns the upsert outcome."""
    repo.upsert_edition(ref)
    content = normalize_fragment(problem.statement_html, problem.source_url)
    return repo.upsert_problem(ref, problem, content, provider="aops_wiki", run_id=run_id)


def add_problem(
    repo: Repository, number: int = 1, html: str = "<p>What is the value of 6 times 7?</p>", key="IMO", year=2020
) -> ProblemRow:
    ref = EditionRef(key, year, f"{year} {key}", "https://example.org/e", None, None)
    store(repo, ref, ParsedProblem(number, html, f"https://example.org/e/{number}", raw_payload=html))
    return repo.get_problem(key, year, number)


# --------------------------------------------------------------------------- fake network clients


class FixtureClient:
    """Stands in for PoliteClient: answers from a routing function, never the network."""

    def __init__(self, route: Callable[[str, Mapping[str, object]], str | bytes | Exception | None]) -> None:
        self.route = route
        self.calls: list[str] = []

    def get(self, url: str, params: Mapping[str, object] | None = None, **_options) -> Response:
        key = cache_key(url, params)
        self.calls.append(key)
        result = self.route(url, dict(params or {}))
        if result is None:
            raise HttpStatusError("HTTP 404", url=key, status=404)
        if isinstance(result, Exception):
            raise result
        content = result.encode("utf-8") if isinstance(result, str) else result
        return Response(key, 200, {"content-type": "text/html; charset=utf-8"}, content)


class DictClient:
    """Stands in for PoliteClient: ``{url: body bytes or exception}``; records every URL asked for."""

    def __init__(self, responses: dict[str, bytes | Exception], content_type: str = "image/png") -> None:
        self.responses = responses
        self.content_type = content_type
        self.calls: list[str] = []

    def get(self, url: str, **_options) -> Response:
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return Response(url, 200, {"content-type": self.content_type}, result)


class FakeResponse:
    """A ``requests.Response`` as far as PoliteClient looks at one."""

    def __init__(self, status=200, body=b"ok", headers=None, url="https://example.org/page"):
        self.status_code = status
        self.content = body
        self.headers = {"content-type": "text/html; charset=utf-8", **(headers or {})}
        self.url = url


class FakeSession:
    """A ``requests.Session`` playing back a script of responses / exceptions; robots.txt is answered separately."""

    def __init__(self, script, robots: str | int = 404):
        self.script = list(script)
        self.robots = robots
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):  # same signature as requests.Session.get
        if url.endswith("/robots.txt"):
            if isinstance(self.robots, int):
                return FakeResponse(self.robots, b"", url=url)
            return FakeResponse(200, self.robots.encode(), {"content-type": "text/plain"}, url=url)
        self.requests.append((url, dict(headers or {})))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --------------------------------------------------------------------------- rendering


def make_png(width: int = 400, height: int = 200) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeRenderer:
    """Stands in for Chromium: returns ``outputs`` in order (bytes, or an exception to raise), then PNGs."""

    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.jobs = []
        self.active = self.peak = 0

    async def render(self, job) -> bytes:
        self.jobs.append(job)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        output = self.outputs.pop(0) if self.outputs else make_png()
        if isinstance(output, Exception):
            raise output
        return output


@pytest.fixture
def chromium_assets_dir() -> Path:
    """The installed pinned render assets; skips the test when Playwright or the assets are missing."""
    pytest.importorskip("playwright")
    from mathbot.render.vendor import missing_assets

    assets_dir = load_settings(dict(os.environ), load_dotenv_file=False).render_assets_dir
    if missing_assets(assets_dir):
        pytest.skip(f"pinned render assets not installed in {assets_dir} (python -m mathbot install-render-assets)")
    return assets_dir


def render_in_chromium(pipeline, settings, assets_dir: Path, problems):
    """Render ``problems`` with a real Chromium; skips the test when Chromium is not installed."""
    from mathbot.render.pipeline import PlaywrightRenderer, RenderError

    async def go():
        try:
            async with PlaywrightRenderer(settings, assets_dir) as renderer:
                return await pipeline.render(problems, renderer)
        except RenderError as exc:
            if "could not start Chromium" in str(exc):
                pytest.skip("Chromium is not installed (python -m playwright install chromium)")
            raise

    return asyncio.run(go())
