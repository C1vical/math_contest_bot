"""Render manifest, asset cache and storage logic. A fake renderer stands in for Chromium;
the one real-browser test is skipped automatically when Chromium is not installed."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import os
from pathlib import Path

import pytest

from mathbot.config import RenderSettings, StorageSettings, load_settings
from mathbot.htmlnorm import normalize_fragment
from mathbot.http import HttpStatusError, Response
from mathbot.models import EditionRef, ParsedProblem
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
from mathbot.render.vendor import ASSET_ORIGIN, missing_assets
from mathbot.validate import validate
from tests.conftest import make_png

SETTINGS = RenderSettings(concurrency=2, retries=1)


def add_problem(repo, number=1, html="<p>What is the value of 6 times 7?</p>", key="IMO", year=2020):
    ref = EditionRef(key, year, f"{year} {key}", "https://example.org/e", None, None)
    repo.upsert_edition(ref)
    parsed = ParsedProblem(number, html, f"https://example.org/e/{number}", raw_payload=html)
    repo.upsert_problem(ref, parsed, normalize_fragment(html, parsed.source_url), provider="aops_wiki")
    return repo.get_problem(key, year, number)


class FakeRenderer:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.jobs: list[RenderJob] = []
        self.active = self.peak = 0

    async def render(self, job: RenderJob) -> bytes:
        self.jobs.append(job)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        output = self.outputs.pop(0) if self.outputs else make_png()
        if isinstance(output, Exception):
            raise output
        return output


def pipeline_for(repo, tmp_path, settings=SETTINGS, assets=None) -> RenderPipeline:
    return RenderPipeline(repo, settings, tmp_path / "renders", assets)


def run(pipeline, problems, renderer):
    return asyncio.run(pipeline.render(problems, renderer))


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
        assert all(url.startswith(ASSET_ORIGIN) for url in __import__("re").findall(r"https://[^'\")]+", doc))
    assert needs_mathjax("\\[a\\]") and not needs_mathjax("$5 and $6")


# --------------------------------------------------------------------------- assets


class AssetClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return Response(url, 200, {"content-type": "image/png"}, result)


IMG = "https://latex.example.org/a/b/c.png"


def test_remote_images_are_cached_by_hash_and_inlined(repo, tmp_path):
    problem = add_problem(repo, html=f'<p>Compute <img src="{IMG}" class="latex" alt="$x$"> please.</p>')
    client = AssetClient({IMG: make_png(20, 10)})
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
    client = AssetClient({IMG: make_png(20, 10)})
    AssetCache(repo, client, tmp_path / "assets", ttl=-1).ensure(IMG)  # stored already expired
    fresh = AssetCache(repo, client, tmp_path / "assets", ttl=3600)
    fresh.ensure(IMG)  # expired -> downloaded again
    fresh.ensure(IMG)  # now fresh -> served from disk
    assert len(client.calls) == 2


def test_failed_or_non_image_assets_fail_the_render_with_a_record(repo, tmp_path):
    problem = add_problem(repo, html=f'<p>Look: <img src="{IMG}" alt="diagram"></p>')
    for response in (HttpStatusError("HTTP 404", url=IMG, status=404), b"<html>Just a moment...</html>", b""):
        cache = AssetCache(repo, AssetClient({IMG: response}), tmp_path / "assets", ttl=3600)
        with pytest.raises(AssetError):
            cache.ensure(IMG)
        assert repo.get_asset(IMG)["status"] == "failed"

    pipeline = pipeline_for(repo, tmp_path, assets=cache)
    renderer = FakeRenderer()
    report = run(pipeline, [problem], renderer)
    assert report.failed == 1 and renderer.jobs == []  # never rendered with a broken image
    assert "asset:" in report.errors[0][1]


def test_failed_downloads_are_not_retried_until_the_negative_cache_expires(repo, tmp_path):
    client = AssetClient({IMG: HttpStatusError("HTTP 403", url=IMG, status=403)})
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
    assert storage.exists("imo/a.png") and storage.get("imo/a.png") == b"data"
    storage.delete("imo/a.png")
    assert not storage.exists("imo/a.png")
    with pytest.raises(StorageError):
        storage.get("imo/a.png")
    with pytest.raises(StorageError):
        storage.put("../escape.png", b"x", "image/png")


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
    assert storage.exists(render_key(problem, pipeline.version))
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

    from mathbot import cli
    from mathbot.contests import ContestRegistry
    from mathbot.db import Repository, open_database

    settings = load_settings({"DATA_DIR": str(tmp_path), "STORAGE_BACKEND": "local"})
    repo = Repository(open_database(settings.database_path))
    repo.sync_registry(ContestRegistry())
    add_problem(repo)
    pipeline = RenderPipeline(repo, SETTINGS, settings.renders_dir)
    run(pipeline, pipeline.plan(), FakeRenderer())
    repo.conn.close()

    assert cli.cmd_upload(argparse.Namespace(force=False), dataclasses.replace(settings, render=SETTINGS)) == 0
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
    assert len(repo.renders_pending_upload(pipeline.version)) == 1


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
    assert not storage.exists(old_key)
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


def _render_assets_dir() -> Path:
    return load_settings(dict(os.environ), load_dotenv_file=False).render_assets_dir


@pytest.mark.browser
def test_real_chromium_renders_tex_offline_and_blocks_network(repo, tmp_path):
    """Renders TeX with the pinned local MathJax, and refuses every remote request."""
    pytest.importorskip("playwright")
    assets_dir = _render_assets_dir()
    if missing_assets(assets_dir):
        pytest.skip(f"pinned render assets not installed in {assets_dir} (python -m mathbot install-render-assets)")

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

    async def go():
        try:
            async with PlaywrightRenderer(settings, assets_dir) as renderer:
                return await pipeline.render([good, remote], renderer)
        except RenderError as exc:
            if "could not start Chromium" in str(exc):
                pytest.skip("Chromium is not installed (python -m playwright install chromium)")
            raise

    report = asyncio.run(go())
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


class CountingClient(AssetClient):
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
    report = run_with(pipeline, [ok, broken], renderer, failures)
    assert (report.rendered, report.failed) == (1, 1)
    assert [job.problem.id for job in renderer.jobs] == [ok.id]
    assert "data:image/png;base64," in renderer.jobs[0].document
    assert report.errors[0] == (broken.id, f"asset: {failures[broken.id]}")


def run_with(pipeline, problems, renderer, failures):
    return asyncio.run(pipeline.render(problems, renderer, prefetch_failures=failures))


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
    assert storage.exists(render_key(new, pipeline.version))


def test_validate_flags_amc_problems_without_answer_choices(repo, tmp_path):
    add_problem(repo, 1, "<p>Compute it. $\textbf{(A) }1\\qquad\textbf{(B) }2$</p>", key="AMC10A", year=2024)
    add_problem(repo, 2, r"<p>Compute it. $(\mathrm{A})\ 3 \qquad (\mathrm{B})\ 4$</p>", key="AMC10A", year=2024)
    add_problem(repo, 3, "<p>A statement that was cut off before its choices.</p>", key="AMC10A", year=2024)
    add_problem(repo, 1, "<p>An olympiad problem has no choices at all.</p>", key="IMO", year=2024)
    report = validate(repo, tmp_path / "renders", "x")
    flagged = [f.message for f in report.warnings if f.check == "no-answer-choices"]
    assert len(flagged) == 1 and flagged[0].startswith("amc10a-2024-p03")


@pytest.mark.browser
def test_real_chromium_typesets_aops_latex_alt_text_and_fails_on_tex_errors(repo, tmp_path):
    pytest.importorskip("playwright")
    assets_dir = _render_assets_dir()
    if missing_assets(assets_dir):
        pytest.skip(f"pinned render assets not installed in {assets_dir} (python -m mathbot install-render-assets)")
    latex = "https://latex.artofproblemsolving.com/a/b/c/abc.png"  # robots-disallowed host: never fetched
    choices = r"$\textbf{(A) }\frac{1}{2}\qquad\textbf{(B) }2$"
    good = add_problem(repo, html=f'<p>Find <img class="latex" src="{latex}" alt="{choices}">.</p>', key="AMC10A")
    table = r"$\notarealmacro{1}$"  # undefined in MathJax: must fail, not render a red box
    broken = add_problem(repo, 2, f'<p>Table: <img class="latex" src="{latex}" alt="{table}"></p>', key="AMC10A")
    settings = RenderSettings(concurrency=1, retries=0)
    pipeline = RenderPipeline(repo, settings, tmp_path / "renders")

    async def go():
        try:
            async with PlaywrightRenderer(settings, assets_dir) as renderer:
                return await pipeline.render([good, broken], renderer)
        except RenderError as exc:
            if "could not start Chromium" in str(exc):
                pytest.skip("Chromium is not installed (python -m playwright install chromium)")
            raise

    report = asyncio.run(go())
    assert report.rendered == 1 and report.failed == 1, report.errors
    assert report.errors[0][0] == broken.id and "TeX error" in report.errors[0][1]
