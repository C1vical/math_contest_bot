"""Render manifest, asset cache and storage logic. A fake renderer stands in for Chromium;
the real-browser tests are skipped automatically when Chromium is not installed."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import re
from pathlib import Path

import pytest

from mathbot.config import RenderSettings, StorageSettings, load_settings
from mathbot.http import HttpStatusError
from mathbot.render.assets import AssetCache, AssetError, sniff_image
from mathbot.render.pipeline import (
    PlaywrightRenderer,
    RenderError,
    RenderJob,
    RenderPipeline,
    render_key,
    validate_png,
)
from mathbot.render.storage import LocalStorage, S3Storage, StorageError
from mathbot.render.template import build_document, needs_mathjax, renderer_version
from mathbot.render.vendor import ASSET_ORIGIN
from mathbot.validate import validate
from tests.conftest import DictClient, FakeRenderer, add_problem, make_png, render_in_chromium, settings_repo

SETTINGS = RenderSettings(concurrency=2, retries=1)


def pipeline_for(repo, tmp_path, settings=SETTINGS, assets=None) -> RenderPipeline:
    return RenderPipeline(repo, settings, tmp_path / "renders", assets)


def run(pipeline, problems, renderer, failures=None):
    return asyncio.run(pipeline.render(problems, renderer, prefetch_failures=failures))


# --------------------------------------------------------------------------- manifest


def test_render_writes_deterministic_path_and_manifest_row(repo, tmp_path):
    problem = add_problem(repo)
    pipeline = pipeline_for(repo, tmp_path)
    report = run(pipeline, pipeline.plan(), FakeRenderer())

    assert (report.planned, report.rendered, report.failed) == (1, 1, 0)
    key = render_key(problem, pipeline.version)
    assert key == f"imo/imo-2020-p01-{problem.content_hash[:12]}-r{pipeline.version}.png"
    assert (tmp_path / "renders" / key).read_bytes().startswith(b"\x89PNG")
    row = repo.current_render(problem.id)
    assert (row["status"], row["local_path"], row["object_key"]) == ("ok", key, key)
    assert (row["width"], row["height"], row["content_hash"]) == (400, 200, problem.content_hash)
    assert row["renderer_version"] == pipeline.version and row["uploaded_at"] is None
    assert not list((tmp_path / "renders").rglob("*.tmp"))  # atomic write leaves no partial files


def test_unchanged_problems_are_not_rendered_again(repo, tmp_path):
    add_problem(repo, 1)
    add_problem(repo, 2, "<p>A second, different problem statement.</p>")
    pipeline = pipeline_for(repo, tmp_path)
    run(pipeline, pipeline.plan(), FakeRenderer())
    assert pipeline.plan() == []


def test_changed_content_is_rendered_again_under_a_new_path(repo, tmp_path):
    old = add_problem(repo)
    pipeline = pipeline_for(repo, tmp_path)
    run(pipeline, pipeline.plan(), FakeRenderer())

    new = add_problem(repo, html="<p>What is the value of 6 times 8?</p>")  # corrected statement
    assert [p.id for p in pipeline.plan()] == [new.id]
    assert repo.current_render(new.id) is None  # the old picture is not served for new content
    run(pipeline, pipeline.plan(), FakeRenderer())
    assert render_key(new, pipeline.version) != render_key(old, pipeline.version)
    assert repo.current_render(new.id)["content_hash"] == new.content_hash


def test_renderer_version_change_invalidates_renders(repo, tmp_path):
    add_problem(repo)
    run(pipeline_for(repo, tmp_path), pipeline_for(repo, tmp_path).plan(), FakeRenderer())
    wider = pipeline_for(repo, tmp_path, dataclasses.replace(SETTINGS, viewport_width=1200))
    assert wider.version != renderer_version(SETTINGS)
    assert len(wider.plan()) == 1


def test_missing_file_is_replanned_even_if_the_manifest_says_ok(repo, tmp_path):
    problem = add_problem(repo)
    pipeline = pipeline_for(repo, tmp_path)
    run(pipeline, pipeline.plan(), FakeRenderer())
    (tmp_path / "renders" / render_key(problem, pipeline.version)).unlink()
    assert [p.id for p in pipeline.plan()] == [problem.id]


def test_failures_are_retried_then_recorded_per_problem(repo, tmp_path):
    good = add_problem(repo, 1)
    bad = add_problem(repo, 2, "<p>This one will fail to render.</p>")
    pipeline = pipeline_for(repo, tmp_path, dataclasses.replace(SETTINGS, concurrency=1))
    renderer = FakeRenderer([make_png(), RenderError("timeout"), b"not a png"])
    report = run(pipeline, [good, bad], renderer)

    assert (report.rendered, report.failed) == (1, 1)
    assert len(renderer.jobs) == 3  # one success + two attempts for the failing problem
    failed = repo.conn.execute("SELECT * FROM renders WHERE problem_id = ?", (bad.id,)).fetchone()
    assert (failed["status"], failed["attempts"], failed["local_path"]) == ("failed", 2, None)
    assert "not a PNG" in failed["error"]
    assert repo.current_render(bad.id) is None
    assert [p.id for p in pipeline.plan()] == [bad.id]  # failed renders are retried next run
    assert not (tmp_path / "renders" / render_key(bad, pipeline.version)).exists()


def test_transient_failure_succeeds_on_retry(repo, tmp_path):
    problem = add_problem(repo)
    pipeline = pipeline_for(repo, tmp_path)
    report = run(pipeline, [problem], FakeRenderer([RenderError("flaky"), make_png()]))
    assert report.rendered == 1 and repo.current_render(problem.id)["attempts"] == 2


def test_concurrency_is_bounded(repo, tmp_path):
    problems = [add_problem(repo, n, f"<p>Statement number {n} of the contest.</p>") for n in range(1, 13)]
    renderer = FakeRenderer()
    report = run(pipeline_for(repo, tmp_path), problems, renderer)
    assert report.rendered == 12 and renderer.peak <= SETTINGS.concurrency


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "not a PNG"),
        (b"<html>error</html>", "not a PNG"),
        (make_png()[:60], "corrupt or truncated"),
        (make_png(10, 10), "implausibly small"),
        (make_png(100, 31_000), "implausibly tall"),
    ],
)
def test_png_validation_rejects_bad_output(data, message):
    with pytest.raises(RenderError, match=message):
        validate_png(data)


def test_png_validation_returns_dimensions():
    assert validate_png(make_png(640, 360)) == (640, 360)


# --------------------------------------------------------------------------- template


def test_document_includes_title_and_only_loads_mathjax_when_needed():
    plain = build_document("2024 AMC 10A Problem 5", "<p>No TeX here.</p>")
    assert '<h2 class="problem-title">2024 AMC 10A Problem 5</h2>' in plain and "mathjax" not in plain.lower()
    tex = build_document("2019 <Euclid> Problem 1", "<p>\\(x^2\\)</p>")
    assert f'<script src="{ASSET_ORIGIN}/mathjax/tex-svg-full.js">' in tex and "2019 &lt;Euclid&gt; Problem 1" in tex
    # every external reference in the page points at the locally served pinned assets
    for doc in (plain, tex):
        assert "cdn.jsdelivr" not in doc and "http://" not in doc
        assert all(url.startswith(ASSET_ORIGIN) for url in re.findall(r"https://[^'\")]+", doc))
    assert needs_mathjax("\\[a\\]") and not needs_mathjax("$5 and $6")


# --------------------------------------------------------------------------- assets


IMG = "https://latex.example.org/a/b/c.png"


def test_remote_images_are_cached_by_hash_and_inlined(repo, tmp_path):
    problem = add_problem(repo, html=f'<p>Compute <img src="{IMG}" class="latex" alt="$x$"> please.</p>')
    client = DictClient({IMG: make_png(20, 10)})
    cache = AssetCache(repo, client, tmp_path / "assets", ttl=3600)

    inlined = cache.inline(problem.id, problem.statement_html)
    encoded = base64.b64encode(make_png(20, 10)).decode()
    assert f'src="data:image/png;base64,{encoded}"' in inlined and IMG not in inlined

    asset = repo.get_asset(IMG)
    assert asset["status"] == "ok" and asset["content_type"] == "image/png" and len(asset["sha256"]) == 64
    assert (tmp_path / "assets" / asset["local_path"]).is_file()
    assert asset["local_path"] == f"{asset['sha256'][:2]}/{asset['sha256']}.png"
    assert repo.conn.execute("SELECT COUNT(*) FROM problem_assets").fetchone()[0] == 1

    cache.inline(problem.id, problem.statement_html)
    assert client.calls == [IMG]  # second use comes from the cache


def test_expired_assets_are_downloaded_again(repo, tmp_path):
    client = DictClient({IMG: make_png(20, 10)})
    AssetCache(repo, client, tmp_path / "assets", ttl=-1).ensure(IMG)  # stored already expired
    fresh = AssetCache(repo, client, tmp_path / "assets", ttl=3600)
    fresh.ensure(IMG)  # expired -> downloaded again
    fresh.ensure(IMG)  # now fresh -> served from disk
    assert len(client.calls) == 2


def test_failed_or_non_image_assets_fail_the_render_with_a_record(repo, tmp_path):
    problem = add_problem(repo, html=f'<p>Look: <img src="{IMG}" alt="diagram"></p>')
    for response in (HttpStatusError("HTTP 404", url=IMG, status=404), b"<html>Just a moment...</html>", b""):
        cache = AssetCache(repo, DictClient({IMG: response}), tmp_path / "assets", ttl=3600)
        with pytest.raises(AssetError):
            cache.ensure(IMG)
        assert repo.get_asset(IMG)["status"] == "failed"

    pipeline = pipeline_for(repo, tmp_path, assets=cache)
    renderer = FakeRenderer()
    report = run(pipeline, [problem], renderer)
    assert report.failed == 1 and renderer.jobs == []  # never rendered with a broken image
    assert "asset:" in report.errors[0][1]


def test_failed_downloads_are_not_retried_until_the_negative_cache_expires(repo, tmp_path):
    client = DictClient({IMG: HttpStatusError("HTTP 403", url=IMG, status=403)})
    cache = AssetCache(repo, client, tmp_path / "assets", ttl=3600)
    for _ in range(3):
        with pytest.raises(AssetError):
            cache.ensure(IMG)
    assert client.calls == [IMG]  # one request, then the cached failure
    repo.conn.execute("UPDATE assets SET expires_at = '2000-01-01T00:00:00Z'")
    client.responses[IMG] = make_png(20, 10)
    assert cache.ensure(IMG)[1] == "image/png" and len(client.calls) == 2


def test_sniff_image_uses_bytes_not_headers():
    assert sniff_image(make_png(), "text/html") == ("image/png", ".png")
    assert sniff_image(b"GIF89a....") == ("image/gif", ".gif")
    assert sniff_image(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>')[0] == "image/svg+xml"
    with pytest.raises(AssetError):
        sniff_image(b"<html></html>", "image/png")


# --------------------------------------------------------------------------- storage / upload / cleanup


def test_local_storage_round_trip_and_path_safety(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    storage.put("imo/a.png", b"data", "image/png")
    assert (tmp_path / "store" / "imo" / "a.png").read_bytes() == b"data"
    storage.delete("imo/a.png")
    assert not (tmp_path / "store" / "imo" / "a.png").exists()
    storage.delete("imo/a.png")  # deleting a missing object is not an error
    with pytest.raises(StorageError):
        storage.put("../escape.png", b"x", "image/png")
    assert not (tmp_path / "escape.png").exists()


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_s3_storage_uses_configured_bucket_and_prefix():
    fake = FakeS3()
    storage = S3Storage(StorageSettings(backend="s3", bucket="my-bucket", key_prefix="renders"), client=fake)
    storage.put("imo/a.png", b"data", "image/png")
    stored = fake.objects[("my-bucket", "renders/imo/a.png")]
    assert stored["ContentType"] == "image/png" and "immutable" in stored["CacheControl"]
    storage.delete("imo/a.png")
    assert fake.objects == {}
    with pytest.raises(StorageError):
        S3Storage(StorageSettings(backend="s3"), client=fake)


def test_upload_is_separate_incremental_and_reset_by_rerender(repo, tmp_path):
    problem = add_problem(repo)
    pipeline = pipeline_for(repo, tmp_path)
    run(pipeline, pipeline.plan(), FakeRenderer())
    storage = LocalStorage(tmp_path / "bucket")

    first = pipeline.upload(storage)
    assert (first.pending, first.uploaded, first.failed) == (1, 1, 0)
    assert (tmp_path / "bucket" / render_key(problem, pipeline.version)).is_file()
    assert repo.current_render(problem.id)["uploaded_at"] is not None
    assert pipeline.upload(storage).pending == 0  # nothing left to upload
    assert pipeline.upload(storage, force=True).uploaded == 1


def test_local_upload_marks_renders_published_without_copying(repo, tmp_path):
    problem = add_problem(repo)
    missing = add_problem(repo, 2, "<p>A render whose file was deleted afterwards.</p>")
    pipeline = pipeline_for(repo, tmp_path)
    run(pipeline, pipeline.plan(), FakeRenderer())
    (tmp_path / "renders" / render_key(missing, pipeline.version)).unlink()
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())

    report = pipeline.upload(None)
    assert (report.pending, report.uploaded, report.failed) == (2, 1, 1) and "missing" in report.errors[0][1]
    assert repo.current_render(problem.id)["uploaded_at"] is not None
    assert repo.current_render(missing.id)["uploaded_at"] is None
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert [p for p in after if p not in before and p.suffix == ".png"] == []  # no second copy of any image


def test_upload_command_copies_nothing_with_local_storage(tmp_path, capsys):
    import argparse

    from mathbot.cli import images as cli_images

    settings = load_settings({"DATA_DIR": str(tmp_path), "STORAGE_BACKEND": "local"})
    with settings_repo(settings) as repo:
        add_problem(repo)
        pipeline = RenderPipeline(repo, SETTINGS, settings.renders_dir)
        run(pipeline, pipeline.plan(), FakeRenderer())

    assert cli_images.cmd_upload(argparse.Namespace(force=False), dataclasses.replace(settings, render=SETTINGS)) == 0
    assert "nothing is copied" in capsys.readouterr().out
    assert not (tmp_path / "object_store").exists()


def test_upload_failure_is_reported_and_left_pending(repo, tmp_path):
    add_problem(repo)
    pipeline = pipeline_for(repo, tmp_path)
    run(pipeline, pipeline.plan(), FakeRenderer())

    class Broken(LocalStorage):
        def put(self, key, data, content_type):
            raise StorageError("bucket unreachable")

    report = pipeline.upload(Broken(tmp_path / "bucket"))
    assert (report.uploaded, report.failed) == (0, 1) and "bucket unreachable" in report.errors[0][1]
    assert len(repo.current_renders(pipeline.version, pending_only=True)) == 1


def test_cleanup_removes_superseded_renders_and_orphans(repo, tmp_path):
    old = add_problem(repo)
    pipeline = pipeline_for(repo, tmp_path)
    run(pipeline, pipeline.plan(), FakeRenderer())
    storage = LocalStorage(tmp_path / "bucket")
    pipeline.upload(storage)
    old_key = render_key(old, pipeline.version)

    new = add_problem(repo, html="<p>The corrected statement of the problem.</p>")
    run(pipeline, pipeline.plan(), FakeRenderer())
    orphan = tmp_path / "renders" / "imo" / "leftover.png"
    orphan.write_bytes(b"junk")

    preview = pipeline.cleanup(storage, dry_run=True)
    assert preview["stale_records"] == 1 and preview["orphan_files"] == 1
    assert (tmp_path / "renders" / old_key).exists() and orphan.exists()

    counts = pipeline.cleanup(storage)
    assert counts == {"stale_records": 1, "local_files": 1, "remote_objects": 1, "orphan_files": 1}
    assert not (tmp_path / "renders" / old_key).exists() and not orphan.exists()
    assert not (tmp_path / "bucket" / old_key).exists()
    assert (tmp_path / "renders" / render_key(new, pipeline.version)).exists()
    assert len(repo.all_renders()) == 1


# --------------------------------------------------------------------------- validate command


def test_validate_reports_missing_renders_and_passes_integrity(repo, tmp_path):
    add_problem(repo, 1)
    add_problem(repo, 2, "<p>Another statement to be rendered.</p>")
    pipeline = pipeline_for(repo, tmp_path)

    before = validate(repo, tmp_path / "renders", pipeline.version)
    assert before.ok and before.summary["renders_missing"] == 2 and before.summary["problems"] == 2
    assert {f.check for f in before.warnings} == {"render-missing"}

    run(pipeline, pipeline.plan(), FakeRenderer([make_png(), RenderError("boom"), RenderError("boom")]))
    after = validate(repo, tmp_path / "renders", pipeline.version)
    assert after.summary["renders_missing"] == 0 and after.summary["renders_failed"] == 1
    assert after.summary["renders_not_uploaded"] == 1
    assert "Result: OK" in after.format()


def test_validate_flags_integrity_problems(repo, tmp_path):
    problem = add_problem(repo, 1)
    add_problem(repo, 3, "<p>Number three, leaving a gap at two.</p>")
    repo.conn.execute("DELETE FROM source_records WHERE problem_id = ?", (problem.id,))
    repo.conn.execute("DELETE FROM problem_versions WHERE problem_id = ?", (problem.id,))
    report = validate(repo, tmp_path / "renders", "x")
    assert not report.ok
    assert {f.check for f in report.errors} == {"missing-provenance", "version-history"}
    assert "numbering-gap" in {f.check for f in report.warnings}
    assert "Result: FAILED" in report.format()


# --------------------------------------------------------------------------- real browser (optional)


@pytest.mark.browser
def test_real_chromium_renders_tex_offline_and_blocks_network(repo, tmp_path, chromium_assets_dir):
    """Renders TeX with the pinned local MathJax, and refuses every remote request."""
    pixel = base64.b64encode(make_png(40, 40)).decode()
    good = add_problem(
        repo,
        html=(
            '<p>If <span class="math inline">\\(x^2 + y^2 = 25\\)</span> and \\(x = 3\\), find \\(y\\).</p>'
            f'<p><img alt="Lightbulb" src="data:image/png;base64,{pixel}"></p>'
        ),
        key="EUCLID",
        year=2024,
    )
    # A remote image that was never inlined must fail the render, not silently load.
    remote = add_problem(
        repo, 2, '<p>A diagram follows.</p><p><img alt="diagram" src="https://example.org/d.png"></p>', "EUCLID", 2024
    )
    settings = RenderSettings(concurrency=1, retries=0)
    pipeline = RenderPipeline(repo, settings, tmp_path / "renders")
    report = render_in_chromium(pipeline, settings, chromium_assets_dir, [good, remote])
    assert report.rendered == 1 and report.failed == 1, report.errors
    assert report.errors[0][0] == remote.id and "image failed to load" in report.errors[0][1]
    row = repo.current_render(good.id)
    assert row["width"] >= 1000 and row["height"] > 100  # 2x device scale of a 1000px-max body


def test_renderer_refuses_to_start_without_pinned_assets(tmp_path):
    async def go():
        async with PlaywrightRenderer(SETTINGS, tmp_path / "empty"):
            pass

    with pytest.raises(RenderError, match="install-render-assets"):
        asyncio.run(go())


# --------------------------------------------------------------------------- prefetch / upload scope


class CountingClient(DictClient):
    def get(self, url, **kwargs):
        if getattr(self, "frozen", False):
            raise AssertionError("network used during the render phase")
        return super().get(url, **kwargs)


def test_remote_images_are_prefetched_before_rendering_and_never_fetched_by_workers(repo, tmp_path):
    ok = add_problem(repo, 1, f'<p>Look at <img src="{IMG}" alt="d"> this.</p>')
    other = "https://latex.example.org/missing.png"
    broken = add_problem(repo, 2, f'<p>And <img src="{other}" alt="e"> that.</p>')
    client = CountingClient({IMG: make_png(20, 10), other: HttpStatusError("HTTP 404", url=other, status=404)})
    pipeline = pipeline_for(repo, tmp_path, assets=AssetCache(repo, client, tmp_path / "assets", ttl=3600))

    failures = pipeline.prefetch([ok, broken])
    assert list(failures) == [broken.id] and "HTTP 404" in failures[broken.id]

    client.frozen = True  # from here on any network call fails the test
    renderer = FakeRenderer()
    report = run(pipeline, [ok, broken], renderer, failures)
    assert (report.rendered, report.failed) == (1, 1)
    assert [job.problem.id for job in renderer.jobs] == [ok.id]
    assert "data:image/png;base64," in renderer.jobs[0].document
    assert report.errors[0] == (broken.id, f"asset: {failures[broken.id]}")


def test_render_without_prefetch_fails_cleanly_instead_of_fetching(repo, tmp_path):
    problem = add_problem(repo, html=f'<p>See <img src="{IMG}" alt="d"> here.</p>')
    client = CountingClient({IMG: make_png(20, 10)})
    client.frozen = True
    pipeline = pipeline_for(repo, tmp_path, assets=AssetCache(repo, client, tmp_path / "assets", ttl=3600))
    report = run(pipeline, [problem], FakeRenderer())
    assert report.failed == 1 and "not in the asset cache" in report.errors[0][1]


def test_force_upload_only_sends_current_renders(repo, tmp_path):
    add_problem(repo)
    pipeline = pipeline_for(repo, tmp_path)
    run(pipeline, pipeline.plan(), FakeRenderer())
    new = add_problem(repo, html="<p>Corrected statement of problem one.</p>")
    run(pipeline, pipeline.plan(), FakeRenderer())
    other_version = pipeline_for(repo, tmp_path, dataclasses.replace(SETTINGS, viewport_width=1200))
    run(other_version, other_version.plan(), FakeRenderer())

    storage = LocalStorage(tmp_path / "bucket")
    report = pipeline.upload(storage, force=True)
    assert report.pending == 1 and report.uploaded == 1  # not the superseded content, not the other version
    assert (tmp_path / "bucket" / render_key(new, pipeline.version)).is_file()


def test_validate_flags_amc_problems_without_answer_choices(repo, tmp_path):
    add_problem(repo, 1, "<p>Compute it. $\textbf{(A) }1\\qquad\textbf{(B) }2$</p>", key="AMC10A", year=2024)
    add_problem(repo, 2, r"<p>Compute it. $(\mathrm{A})\ 3 \qquad (\mathrm{B})\ 4$</p>", key="AMC10A", year=2024)
    add_problem(repo, 3, "<p>A statement that was cut off before its choices.</p>", key="AMC10A", year=2024)
    add_problem(repo, 1, "<p>An olympiad problem has no choices at all.</p>", key="IMO", year=2024)
    report = validate(repo, tmp_path / "renders", "x")
    flagged = [f.message for f in report.warnings if f.check == "no-answer-choices"]
    assert len(flagged) == 1 and flagged[0].startswith("amc10a-2024-p03")


@pytest.mark.browser
def test_real_chromium_typesets_aops_latex_alt_text_and_fails_on_tex_errors(repo, tmp_path, chromium_assets_dir):
    latex = "https://latex.artofproblemsolving.com/a/b/c/abc.png"  # robots-disallowed host: never fetched
    choices = r"$\textbf{(A) }\frac{1}{2}\qquad\textbf{(B) }2$"
    good = add_problem(repo, html=f'<p>Find <img class="latex" src="{latex}" alt="{choices}">.</p>', key="AMC10A")
    table = r"$\notarealmacro{1}$"  # undefined in MathJax: must fail, not render a red box
    broken = add_problem(repo, 2, f'<p>Table: <img class="latex" src="{latex}" alt="{table}"></p>', key="AMC10A")
    settings = RenderSettings(concurrency=1, retries=0)
    pipeline = RenderPipeline(repo, settings, tmp_path / "renders")
    report = render_in_chromium(pipeline, settings, chromium_assets_dir, [good, broken])
    assert report.rendered == 1 and report.failed == 1, report.errors
    assert report.errors[0][0] == broken.id and "TeX error" in report.errors[0][1]


# --------------------------------------------------------------------------- Playwright renderer (fake browser objects)


def test_renderer_closes_the_browser_when_setup_fails_part_way(tmp_path, monkeypatch):
    """__aexit__ never runs when __aenter__ raises, so a failed setup must clean up itself."""
    playwright_api = pytest.importorskip("playwright.async_api")

    class Browser:
        closed = False

        async def new_context(self, **_options):
            raise RuntimeError("context refused")

        async def close(self):
            self.closed = True

    class Playwright:
        def __init__(self):
            self.browser, self.stopped = Browser(), False
            self.chromium = self

        async def launch(self, **_options):
            return self.browser

        async def stop(self):
            self.stopped = True

        async def start(self):
            return self

    fake = Playwright()
    monkeypatch.setattr(playwright_api, "async_playwright", lambda: fake)
    monkeypatch.setattr("mathbot.render.pipeline.require_assets", lambda root: None)

    async def go():
        async with PlaywrightRenderer(SETTINGS, tmp_path):
            pass

    with pytest.raises(RuntimeError, match="context refused"):
        asyncio.run(go())
    assert fake.browser.closed and fake.stopped


class Page:
    def __init__(self, ready_after: float = 0.0):
        self.ready_after = ready_after
        self.closed = False

    async def set_content(self, html, **_options):
        pass

    async def evaluate(self, script, *_args):
        await asyncio.sleep(self.ready_after)

    async def close(self):
        self.closed = True


def test_a_page_that_never_becomes_ready_times_out_and_is_replaced(tmp_path):
    """RENDER_TIMEOUT_MS bounds the wait for MathJax / images too, not only Playwright's own calls."""
    settings = dataclasses.replace(SETTINGS, timeout_ms=50)
    renderer = PlaywrightRenderer(settings, tmp_path)
    hanging, fresh = Page(ready_after=5), Page()

    async def new_page():
        return fresh

    async def go():
        renderer._context = type("Context", (), {"new_page": staticmethod(new_page)})()
        renderer._pages = asyncio.Queue()
        renderer._pages.put_nowait(hanging)
        with pytest.raises(RenderError, match="not ready after 50 ms"):
            await asyncio.wait_for(renderer.render(RenderJob(None, "<p>x</p>", True)), 2)
        return renderer._pages.get_nowait()

    assert asyncio.run(go()) is fresh and hanging.closed  # the stuck page is not reused

    with pytest.raises(RuntimeError, match="outside 'async with'"):
        asyncio.run(PlaywrightRenderer(settings, tmp_path).render(RenderJob(None, "<p>x</p>", True)))


def test_an_image_that_cannot_be_saved_fails_only_that_problem(repo, tmp_path, monkeypatch):
    """One locked file used to end the whole render run (and close the browser under the others)."""
    from mathbot.render import pipeline as pipeline_module

    locked, saved = add_problem(repo, 1), add_problem(repo, 2, "<p>A second statement to render.</p>")
    real_write = pipeline_module.atomic_write_bytes

    def write(path, data):
        if path.name.startswith(locked.id):
            raise PermissionError(13, "Access is denied")
        real_write(path, data)

    monkeypatch.setattr(pipeline_module, "atomic_write_bytes", write)
    pipeline = pipeline_for(repo, tmp_path, dataclasses.replace(SETTINGS, concurrency=1))
    report = run(pipeline, [locked, saved], FakeRenderer())
    assert (report.rendered, report.failed) == (1, 1)
    assert report.errors[0][0] == locked.id and "could not save the image" in report.errors[0][1]
    assert repo.current_render(saved.id) is not None and repo.current_render(locked.id) is None


def test_cleanup_keeps_an_older_renderers_picture_until_it_is_replaced(repo, tmp_path):
    """Deleting old-version images before the new version has rendered them leaves the bot with text only."""
    waiting, replaced = add_problem(repo, 1), add_problem(repo, 2, "<p>A second statement to render.</p>")
    old = pipeline_for(repo, tmp_path)
    run(old, old.plan(), FakeRenderer())
    new = pipeline_for(repo, tmp_path, dataclasses.replace(SETTINGS, viewport_width=1200))
    run(new, [replaced], FakeRenderer())

    counts = new.cleanup()
    assert counts == {"stale_records": 1, "local_files": 1, "remote_objects": 0, "orphan_files": 0}
    assert (tmp_path / "renders" / render_key(waiting, old.version)).is_file()
    assert not (tmp_path / "renders" / render_key(replaced, old.version)).exists()
    assert repo.current_render(waiting.id)["renderer_version"] == old.version  # still what the bot serves
    assert repo.current_render(replaced.id)["renderer_version"] == new.version

    failed = add_problem(repo, 3, "<p>A third statement whose render fails.</p>")
    run(old, [failed], FakeRenderer([RenderError("boom"), RenderError("boom")]))
    assert new.cleanup()["stale_records"] == 1  # a failed attempt has no picture to keep


def test_riff_files_are_only_images_when_they_are_webp():
    assert sniff_image(b"RIFF\x10\x00\x00\x00WEBPVP8 ") == ("image/webp", ".webp")
    with pytest.raises(AssetError):
        sniff_image(b"RIFF\x10\x00\x00\x00WAVEfmt ")  # audio, not an image


class _BrokenPage:
    """A Playwright page that has crashed: every call fails."""

    def __init__(self):
        self.closed = False

    async def set_content(self, *_args, **_kwargs):
        raise RuntimeError("Target page, context or browser has been closed")

    async def close(self):
        self.closed = True


class _FreshContext:
    def __init__(self):
        self.opened = 0

    async def new_page(self):
        self.opened += 1
        return object()


def test_a_crashed_page_is_replaced_instead_of_poisoning_the_pool():
    from mathbot.render.pipeline import PlaywrightRenderer, RenderError, RenderJob

    renderer = PlaywrightRenderer(load_settings({}).render, Path("unused"))
    broken, context = _BrokenPage(), _FreshContext()

    async def go():
        renderer._context = context
        renderer._pages = asyncio.Queue()
        renderer._pages.put_nowait(broken)
        with pytest.raises(RenderError, match="closed"):
            await renderer.render(RenderJob(problem=None, document="<p>x</p>", uses_mathjax=False))
        return renderer._pages.get_nowait()

    replacement = asyncio.run(go())
    assert broken.closed and context.opened == 1 and replacement is not broken


def test_vendored_files_are_read_and_verified_once(tmp_path, monkeypatch):
    from mathbot.render import vendor

    item = vendor.VENDORED[1]
    data = b"font bytes"
    (tmp_path / item.path).parent.mkdir(parents=True)
    (tmp_path / item.path).write_bytes(data)
    monkeypatch.setattr(
        vendor, "BY_PATH", {item.path: vendor.VendoredFile(item.path, item.url, vendor._sha256(data), "font/woff")}
    )
    reads = []
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or original(self))
    cache = vendor.AssetReader(tmp_path)
    assert cache.read(item.path) == (data, "font/woff")
    assert cache.read(item.path) == (data, "font/woff")
    assert cache.read("not/pinned.js") is None
    assert len(reads) == 1


def test_an_unexpected_error_building_one_job_fails_only_that_problem(repo, tmp_path):
    good, bad = add_problem(repo, number=1), add_problem(repo, number=2)
    pipeline = RenderPipeline(repo, load_settings({}).render, tmp_path / "renders")
    original = pipeline._job

    def job(problem):
        if problem.id == bad.id:
            raise ValueError("malformed statement")
        return original(problem)

    pipeline._job = job
    report = asyncio.run(pipeline.render([good, bad], FakeRenderer()))
    assert (report.rendered, report.failed) == (1, 1)
    assert "malformed statement" in report.errors[0][1]


def test_cleanup_reports_a_storage_failure_instead_of_crashing(tmp_path, monkeypatch, capsys):
    import argparse

    from mathbot.cli import images as cli_images
    from mathbot.render import storage as storage_module

    env = {"DATA_DIR": str(tmp_path), "STORAGE_BACKEND": "s3", "S3_BUCKET": "b"}
    settings = load_settings({**env, "S3_ACCESS_KEY_ID": "k", "S3_SECRET_ACCESS_KEY": "s"})
    with settings_repo(settings) as repo:
        old = add_problem(repo)
        pipeline = RenderPipeline(repo, settings.render, settings.renders_dir)
        asyncio.run(pipeline.render([old], FakeRenderer()))
        pipeline.upload(LocalStorage(tmp_path / "bucket"))
        add_problem(repo, html="<p>A corrected statement of the problem.</p>")  # the old render is now stale

    class Unreachable(LocalStorage):
        def delete(self, key):
            raise StorageError("bucket unreachable")

    monkeypatch.setattr(storage_module, "build_storage", lambda _settings: Unreachable(tmp_path / "bucket"))
    assert cli_images.cmd_cleanup(argparse.Namespace(dry_run=False, remote=True), settings) == 1
    assert "bucket unreachable" in capsys.readouterr().err


@pytest.mark.browser
def test_real_chromium_shrinks_over_wide_content_instead_of_cutting_it_off(repo, tmp_path, chromium_assets_dir):
    """A block wider than the page (like a long display equation, it cannot wrap) must be
    scaled into the picture: the red marker at its right end has to be in the PNG."""
    from PIL import Image

    wide = add_problem(
        repo,
        html='<p>Use the figure below.</p><div style="width:1660px">'
        '<div style="margin-left:1600px;width:60px;height:60px;background:#ff0000"></div></div>',
    )
    settings = RenderSettings(concurrency=1, retries=0)
    pipeline = RenderPipeline(repo, settings, tmp_path / "renders")
    report = render_in_chromium(pipeline, settings, chromium_assets_dir, [wide])
    assert report.rendered == 1, report.errors
    image = Image.open(tmp_path / "renders" / repo.current_render(wide.id)["local_path"]).convert("RGB")
    assert image.width == settings.viewport_width * settings.device_scale_factor
    red = sum(1 for r, g, b in image.getdata() if r > 200 and g < 60 and b < 60)
    assert red > 100  # the marker cell survived (it used to fall outside the screenshot)


def test_uploads_run_in_parallel_and_each_is_recorded(repo, tmp_path):
    """Object storage is latency-bound (about 0.5 s per PUT to R2), so uploads overlap."""
    import threading
    import time

    problems = [add_problem(repo, n, f"<p>Problem number {n} of the parallel test.</p>") for n in range(1, 13)]
    pipeline = RenderPipeline(repo, load_settings({}).render, tmp_path / "renders")
    asyncio.run(pipeline.render(problems, FakeRenderer()))

    class SlowStorage(LocalStorage):
        def __init__(self, root):
            super().__init__(root)
            self.active = self.peak = 0
            self.lock = threading.Lock()

        def put(self, key, data, content_type, *, cache_control=None):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.05)
            if key.endswith(problems[3].content_hash[:12] + f"-r{pipeline.version}.png"):
                with self.lock:
                    self.active -= 1
                raise StorageError("bucket hiccup")
            super().put(key, data, content_type)
            with self.lock:
                self.active -= 1

    storage = SlowStorage(tmp_path / "bucket")
    report = pipeline.upload(storage)
    assert (report.uploaded, report.failed) == (11, 1) and storage.peak > 1
    pending = repo.current_renders(pipeline.version, pending_only=True)
    assert [r["problem_id"] for r in pending] == [problems[3].id]  # only the failed one is retried
