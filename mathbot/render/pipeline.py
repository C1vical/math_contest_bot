"""Render, upload and clean up problem images, driven by the ``renders`` manifest.

A render is identified by ``(problem id, content hash, renderer version)``:

* unchanged problems are never rendered twice;
* a corrected statement (new content hash) or a template change (new renderer
  version) is picked up automatically as "missing";
* output paths are deterministic and embed the hash, so a new render never
  overwrites the one the bot may currently be serving;
* PNGs are validated and written atomically - a partial file is never
  recorded as complete; failures are recorded per problem with the error.

Statements are rendered as stored. Every remote image (AoPS formula PNGs,
Asymptote diagrams) is downloaded once by :meth:`RenderPipeline.prefetch`,
before the browser starts, and inlined as a ``data:`` URI. A formula whose PNG
can't be downloaded is typeset from its alt TeX instead (``render/aops_tex.py``);
any other missing image fails that problem's render.

Rendering (local) and uploading (remote) are separate steps so either can be
retried on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mathbot.config import RenderSettings
from mathbot.db.repo import ProblemRow, Repository
from mathbot.htmlnorm import remote_images
from mathbot.http import atomic_write_bytes
from mathbot.render import PNG_SIGNATURE
from mathbot.render.aops_tex import formula_images, latex_images_to_tex
from mathbot.render.assets import AssetCache, AssetError
from mathbot.render.storage import Storage, StorageError
from mathbot.render.template import READY_SCRIPT, build_document, needs_mathjax, renderer_version
from mathbot.render.vendor import ASSET_ORIGIN, AssetReader, RenderAssetsMissing, require_assets

log = logging.getLogger(__name__)

MIN_WIDTH, MIN_HEIGHT, MAX_HEIGHT = 100, 40, 30_000
VIEWPORT_HEIGHT = 800  # the screenshot is of <body>, so this only sets the initial layout
MAX_REPORTED_ERRORS = 40  # failures listed in a report; the rest are in the renders table
PROGRESS_EVERY_RENDERS = 25
PROGRESS_EVERY_PREFETCHES = 250


class RenderError(Exception):
    """A problem could not be rendered to a valid PNG."""


@dataclass(frozen=True)
class RenderJob:
    problem: ProblemRow
    document: str
    uses_mathjax: bool


@dataclass
class RenderReport:
    planned: int = 0
    rendered: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def format(self) -> str:
        lines = [f"Render report: {self.planned} needed, {self.rendered} rendered, {self.failed} failed"]
        lines += [f"  {pid}: {error}" for pid, error in self.errors[:MAX_REPORTED_ERRORS]]
        if len(self.errors) > MAX_REPORTED_ERRORS:
            lines.append(f"  ... {len(self.errors) - MAX_REPORTED_ERRORS} more (see renders table)")
        return "\n".join(lines)


@dataclass
class UploadReport:
    pending: int = 0
    uploaded: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def format(self) -> str:
        lines = [f"Upload report: {self.pending} pending, {self.uploaded} uploaded, {self.failed} failed"]
        lines += [f"  {key}: {error}" for key, error in self.errors[:MAX_REPORTED_ERRORS]]
        if len(self.errors) > MAX_REPORTED_ERRORS:
            lines.append(f"  ... {len(self.errors) - MAX_REPORTED_ERRORS} more")
        return "\n".join(lines)


def render_key(problem: ProblemRow, version: str) -> str:
    """Deterministic relative path / object key, e.g. ``amc10a/amc10a-2024-p05-1a2b3c4d5e6f-r2-w1000-s2.png``."""
    return f"{problem.series_key.lower()}/{problem.id}-{problem.content_hash[:12]}-r{version}.png"


def validate_png(data: bytes) -> tuple[int, int]:
    """Check the bytes are a complete, sanely sized PNG and return ``(width, height)``."""
    if not data.startswith(PNG_SIGNATURE):
        raise RenderError("output is not a PNG")
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()  # forces a full decode, catching truncated files
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise RenderError(f"PNG is corrupt or truncated: {exc}") from exc
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise RenderError(f"image is implausibly small ({width}x{height})")
    if height > MAX_HEIGHT:
        raise RenderError(f"image is implausibly tall ({width}x{height})")
    return width, height


def _prefetch_images(assets: AssetCache, html: str) -> str | None:
    """Download the remote images of one statement. Returns why it can't be rendered yet, or None."""
    optional = formula_images(html)
    for url in remote_images(html):
        try:
            assets.ensure(url)
        except AssetError as exc:
            if url in optional and not exc.retryable:
                continue  # typeset from its alt TeX instead
            return str(exc)
    return None


class PageRenderer(Protocol):
    """Turns one HTML document into PNG bytes. The Playwright implementation is below."""

    async def render(self, job: RenderJob) -> bytes: ...


class PlaywrightRenderer:
    """One Chromium instance with a fixed pool of reusable pages (bounded concurrency)."""

    def __init__(self, settings: RenderSettings, assets_dir: Path) -> None:
        self.settings = settings
        self.assets_dir = assets_dir
        self._files = AssetReader(assets_dir)
        self._playwright = None
        self._browser = None
        self._context = None
        self._pages: asyncio.Queue | None = None

    async def __aenter__(self) -> PlaywrightRenderer:
        from playwright.async_api import async_playwright

        try:
            require_assets(self.assets_dir)
        except RenderAssetsMissing as exc:
            raise RenderError(str(exc)) from exc
        self._playwright = await async_playwright().start()
        try:
            try:
                self._browser = await self._playwright.chromium.launch(headless=True)
            except Exception as exc:  # Playwright raises its own Error types; any of them means "no browser"
                raise RenderError(
                    f"could not start Chromium ({exc}). Install it with: python -m playwright install chromium"
                ) from exc
            self._context = await self._browser.new_context(
                device_scale_factor=self.settings.device_scale_factor,
                viewport={"width": self.settings.viewport_width, "height": VIEWPORT_HEIGHT},
                java_script_enabled=True,
            )
            self._context.set_default_timeout(self.settings.timeout_ms)
            # Pinned MathJax / fonts come from disk; every other request is refused, so a render
            # can never depend on (or leak to) the network. Statement images are data: URIs.
            await self._context.route("**/*", self._route)
            self._pages = asyncio.Queue()
            for _ in range(self.settings.concurrency):
                self._pages.put_nowait(await self._context.new_page())
        except BaseException:
            # __aexit__ is not called when __aenter__ fails, so close what was started here.
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = self._context = self._pages = None
        try:
            if browser is not None:
                await browser.close()
        finally:
            if playwright is not None:
                await playwright.stop()

    async def _route(self, route) -> None:
        url = route.request.url
        if url.startswith(ASSET_ORIGIN + "/"):
            found = self._files.read(url[len(ASSET_ORIGIN) + 1 :].split("?", 1)[0])
            if found is not None:
                body, content_type = found
                await route.fulfill(status=200, body=body, content_type=content_type)
                return
        log.warning("render page requested a non-local resource; blocked", extra={"url": url[:200]})
        await route.abort("blockedbyclient")

    async def render(self, job: RenderJob) -> bytes:
        if self._pages is None or self._context is None:
            raise RuntimeError("PlaywrightRenderer used outside 'async with'")
        pages = self._pages
        page = await pages.get()
        try:
            await page.set_content(job.document, wait_until="load")
            # page.evaluate has no timeout of its own: without this, a page whose MathJax or
            # images never settle would hang the whole run.
            await asyncio.wait_for(page.evaluate(READY_SCRIPT, job.uses_mathjax), self.settings.timeout_ms / 1000)
            return await page.locator("body").screenshot(type="png", animations="disabled")
        except asyncio.TimeoutError as exc:
            page = await self._replace_page(page)  # its script may still be running
            raise RenderError(f"page was not ready after {self.settings.timeout_ms} ms") from exc
        except Exception as exc:  # Playwright's errors, reported per problem
            # The page may have crashed or been closed; returning it to the pool would fail
            # every later render on it, so a fresh page takes its place.
            page = await self._replace_page(page)
            raise RenderError(f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}") from exc
        finally:
            pages.put_nowait(page)

    async def _replace_page(self, page):
        """Close a page left in an unknown state and open a fresh one in its place."""
        with contextlib.suppress(Exception):  # it may already be closed or crashed
            await page.close()
        try:
            return await self._context.new_page()
        except Exception as exc:  # keep the closed page: renders on it fail and are recorded per problem
            log.warning("could not open a replacement browser page", extra={"error": str(exc)})
            return page


class RenderPipeline:
    """Plans, renders, uploads and cleans up problem images for one renderer version."""

    def __init__(
        self,
        repo: Repository,
        settings: RenderSettings,
        renders_dir: Path,
        assets: AssetCache | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.renders_dir = renders_dir
        self.assets = assets
        self.version = renderer_version(settings)
        self.progress = progress or (lambda _message: None)

    # -- planning

    def plan(self, series_keys: Sequence[str] | None = None, *, limit: int | None = None) -> list[ProblemRow]:
        """Problems whose current content has no successful render, plus any whose file vanished."""
        missing = self.repo.problems_needing_render(self.version, series_keys=series_keys)
        seen = {p.id for p in missing}
        for problem in self.repo.iter_problems(series_keys):
            if problem.id in seen:
                continue
            if not (self.renders_dir / render_key(problem, self.version)).is_file():
                missing.append(problem)
        return missing[:limit] if limit else missing

    def _job(self, problem: ProblemRow) -> RenderJob:
        """Build the page from cached assets only: no network I/O inside the render workers.

        Formula images that are cached stay as AoPS drew them; the others are typeset
        from their alt TeX. Every other image must be cached, or this raises AssetError.
        """
        assets = self.assets
        body, _ = latex_images_to_tex(problem.statement_html, keep=assets.has if assets else None)
        if assets is not None:
            body = assets.inline(problem.id, body, fetch=False)
        return RenderJob(problem, build_document(problem.title, body), needs_mathjax(body))

    # -- rendering

    def _fail(self, problem: ProblemRow, error: str, attempts: int, report: RenderReport) -> None:
        self.repo.record_render(
            problem_id=problem.id,
            content_hash=problem.content_hash,
            renderer_version=self.version,
            status="failed",
            attempts=attempts,
            error=error,
        )
        report.failed += 1
        report.errors.append((problem.id, error))
        log.error("render failed", extra={"problem": problem.id, "error": error, "attempts": attempts})

    def prefetch(self, problems: Sequence[ProblemRow]) -> dict[str, str]:
        """Download every remote image before rendering. Returns ``{problem id: error}``.

        A problem fails here when an image it can't do without (a diagram, a non-TeX
        image) is unavailable, or when any image may still become available (network
        error, 5xx, robots.txt refusal): a render made with a stand-in would never be
        revisited, so the problem waits for the next run instead. A formula image
        that is permanently unavailable (404, challenge) is fine: :meth:`_job`
        typesets its alt TeX.

        Plain blocking I/O, deliberately kept out of the async render phase so slow
        image hosts cannot stall the event loop or serialize the browser workers.
        """
        failures: dict[str, str] = {}
        if self.assets is None:
            return failures
        for index, problem in enumerate(problems, start=1):
            error = _prefetch_images(self.assets, problem.statement_html)
            if error is not None:
                failures[problem.id] = error
            if index % PROGRESS_EVERY_PREFETCHES == 0:
                self.progress(f"images checked for {index}/{len(problems)} problems")
        return failures

    async def render(
        self,
        problems: Sequence[ProblemRow],
        renderer: PageRenderer,
        *,
        prefetch_failures: dict[str, str] | None = None,
    ) -> RenderReport:
        """Render ``problems``. Call :meth:`prefetch` first when statements contain remote images."""
        report = RenderReport(planned=len(problems))
        failed_assets = prefetch_failures or {}
        queue: asyncio.Queue[ProblemRow] = asyncio.Queue()
        for problem in problems:
            if problem.id in failed_assets:
                self._fail(problem, f"asset: {failed_assets[problem.id]}", 1, report)
            else:
                queue.put_nowait(problem)

        async def worker() -> None:
            while True:
                try:
                    problem = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await self._render_one(problem, renderer, report)

        # A fixed number of workers pulls from the queue: never one task per problem.
        await asyncio.gather(*(worker() for _ in range(min(self.settings.concurrency, max(1, len(problems))))))
        return report

    async def _render_one(self, problem: ProblemRow, renderer: PageRenderer, report: RenderReport) -> None:
        try:
            job = self._job(problem)
        except AssetError as exc:
            self._fail(problem, f"asset: {exc}", 1, report)
            return
        except Exception as exc:  # unexpected markup: fail this problem, keep the run going
            log.exception("could not build the render page", extra={"problem": problem.id})
            self._fail(problem, f"could not build the page: {type(exc).__name__}: {exc}", 1, report)
            return

        attempts = self.settings.retries + 1
        error = "unknown error"
        for attempt in range(1, attempts + 1):
            try:
                data = await renderer.render(job)
                width, height = validate_png(data)
            except RenderError as exc:
                error = str(exc)
                log.warning("render attempt failed", extra={"problem": problem.id, "attempt": attempt, "error": error})
                continue
            key = render_key(problem, self.version)
            try:
                atomic_write_bytes(self.renders_dir / key, data)
            except OSError as exc:  # e.g. the file stayed locked: fail this problem, keep the run going
                self._fail(problem, f"could not save the image: {exc}", attempt, report)
                return
            self.repo.record_render(
                problem_id=problem.id,
                content_hash=problem.content_hash,
                renderer_version=self.version,
                status="ok",
                local_path=key,
                object_key=key,
                width=width,
                height=height,
                size_bytes=len(data),
                attempts=attempt,
            )
            report.rendered += 1
            if report.rendered % PROGRESS_EVERY_RENDERS == 0:
                self.progress(f"rendered {report.rendered}/{report.planned}")
            return
        self._fail(problem, error, attempts, report)

    # -- upload / cleanup

    def upload(self, storage: Storage | None, *, force: bool = False) -> UploadReport:
        """Publish current renders to ``storage``.

        ``storage=None`` is local mode: the renders directory itself is what the bot
        serves, so nothing is copied and each render whose file exists is just marked
        as published.
        """
        rows = [r for r in self.repo.current_renders(self.version, pending_only=not force) if r["object_key"]]
        report = UploadReport(pending=len(rows))
        for row in rows:
            key = row["object_key"]
            path = self.renders_dir / row["local_path"]
            try:
                if storage is None:
                    if not path.is_file():
                        raise OSError(f"render file is missing: {path}")
                else:
                    data = path.read_bytes()
                    validate_png(data)
                    storage.put(key, data, "image/png")
            except (OSError, RenderError, StorageError) as exc:
                report.failed += 1
                report.errors.append((key, str(exc)))
                log.error("upload failed", extra={"key": key, "error": str(exc)})
                continue
            self.repo.mark_uploaded(row["id"])
            report.uploaded += 1
        return report

    def cleanup(self, storage: Storage | None = None, *, dry_run: bool = False) -> dict[str, int]:
        """Remove renders that are no longer needed, and orphan files.

        That is renders of superseded content, failed attempts, and renders by another
        renderer version once this version has rendered the same content. An older
        version's picture that has no replacement yet is kept: the bot still serves it.
        """
        counts = {"stale_records": 0, "local_files": 0, "remote_objects": 0, "orphan_files": 0}
        for row in self.repo.stale_renders(self.version):
            counts["stale_records"] += 1
            if dry_run:
                continue
            if row["local_path"]:
                path = self.renders_dir / row["local_path"]
                if path.is_file():
                    path.unlink()
                    counts["local_files"] += 1
            if storage is not None and row["uploaded_at"] and row["object_key"]:
                storage.delete(row["object_key"])
                counts["remote_objects"] += 1
            self.repo.delete_render(row["id"])

        known = {r["local_path"] for r in self.repo.all_renders() if r["local_path"]}
        if self.renders_dir.is_dir():
            for path in self.renders_dir.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.renders_dir).as_posix()
                if relative not in known or path.suffix == ".tmp":
                    counts["orphan_files"] += 1
                    if not dry_run:
                        path.unlink()
        return counts
