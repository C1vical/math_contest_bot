"""AoPS statements are rendered with AoPS's own PNGs; ROBOTS_OVERRIDE_HOSTS is the user's opt-in.

Fake clients only: nothing here touches the network.
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

from mathbot import cli
from mathbot.config import HttpSettings, RenderSettings, load_settings
from mathbot.db import Repository, open_database
from mathbot.http import HttpStatusError, PoliteClient, RobotsDisallowedError, SourceBlockedError, TransportError
from mathbot.render.assets import AssetCache, AssetError
from mathbot.render.pipeline import RenderPipeline
from mathbot.render.template import TEMPLATE_VERSION, renderer_version
from tests.conftest import make_png
from tests.test_http import FakeResponse, FakeSession
from tests.test_render import SETTINGS, AssetClient, FakeRenderer, add_problem

LATEX = "https://latex.artofproblemsolving.com/a/b/c/formula.png"
ASY = "https://latex.artofproblemsolving.com/d/e/f/diagram.png"
LATEX_HOST = "latex.artofproblemsolving.com"


# --------------------------------------------------------------------------- settings


def test_override_and_asset_interval_settings():
    settings = load_settings({"ROBOTS_OVERRIDE_HOSTS": f" {LATEX_HOST} , Example.org ", "ASSET_MIN_INTERVAL": "0.25"})
    assert settings.http.robots_override_hosts == {LATEX_HOST, "example.org"}
    assert settings.http.asset_min_interval == 0.25 and settings.http.min_interval == 1.0
    defaults = load_settings({})
    assert defaults.http.robots_override_hosts == frozenset() and defaults.http.asset_min_interval == 1.0
    assert not hasattr(defaults.render, "aops_formula_mode")  # one mode only: native images


def test_image_downloads_are_paced_by_asset_min_interval(tmp_path):
    settings = load_settings({"DATA_DIR": str(tmp_path), "HTTP_MIN_INTERVAL": "2", "ASSET_MIN_INTERVAL": "0.25"})
    assert cli._asset_client(settings).settings.min_interval == 0.25
    assert cli._client(settings).settings.min_interval == 2.0  # page scraping keeps its own pace


def test_renderer_version_changed_so_everything_is_rendered_again():
    version = renderer_version(SETTINGS)
    assert version == f"{TEMPLATE_VERSION}-w1000-s2"
    assert version not in {"5-w1000-s2", "5-img-w1000-s2"}  # the previous TeX / image modes


# --------------------------------------------------------------------------- robots override


DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def client_for(session, **overrides):
    settings = HttpSettings(min_interval=0, max_retries=0, **overrides)
    return PoliteClient(settings, session=session, sleep=lambda s: None)


def test_robots_is_enforced_unless_the_host_is_listed():
    session = FakeSession([FakeResponse(body=b"png")], robots=DISALLOW_ALL)
    with pytest.raises(RobotsDisallowedError):
        client_for(session).get(LATEX)
    assert session.requests == []

    session = FakeSession([FakeResponse(body=b"png")], robots=DISALLOW_ALL)
    client = client_for(session, robots_override_hosts=frozenset({LATEX_HOST}))
    assert client.get(LATEX).content == b"png"
    with pytest.raises(RobotsDisallowedError):  # other hosts are unaffected
        client.get("https://other.example.org/x.png")


def test_override_logs_one_warning_naming_the_host(caplog):
    session = FakeSession([FakeResponse(body=b"png"), FakeResponse(body=b"png")], robots=DISALLOW_ALL)
    client = client_for(session, robots_override_hosts=frozenset({LATEX_HOST}))
    with caplog.at_level("WARNING", logger="mathbot.http"):
        client.get(LATEX)
        client.get(ASY)
    warnings = [r for r in caplog.records if "ROBOTS_OVERRIDE_HOSTS" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].host == LATEX_HOST


def test_override_never_bypasses_an_anti_bot_challenge():
    challenge = FakeResponse(403, b"<title>Just a moment...</title>", {"cf-mitigated": "challenge"})
    session = FakeSession([challenge], robots=DISALLOW_ALL)
    client = client_for(session, robots_override_hosts=frozenset({"artofproblemsolving.com"}))
    with pytest.raises(SourceBlockedError):
        client.get("https://artofproblemsolving.com/wiki/images/a/a1/x.png")


# --------------------------------------------------------------------------- rendering


def statement():
    return (
        f'<p>Find <img class="latex" src="{LATEX}" alt="$x^2$"> here.</p>'
        f'<p><img class="latexcenter" src="{ASY}" alt="[asy] draw((0,0)--(1,1)); [/asy]"></p>'
    )


def pipeline(repo, tmp_path, responses, settings: RenderSettings = SETTINGS):
    client = AssetClient(responses)
    return RenderPipeline(repo, settings, tmp_path / "renders", AssetCache(repo, client, tmp_path / "a", 3600)), client


def render(p, problems):
    renderer = FakeRenderer()
    failures = p.prefetch(problems)
    report = asyncio.run(p.render(problems, renderer, prefetch_failures=failures))
    return report, failures, renderer


def test_native_formula_and_diagram_pngs_are_kept_and_inlined(repo, tmp_path):
    problem = add_problem(repo, html=statement(), key="AMC10A", year=2024)
    p, client = pipeline(repo, tmp_path, {LATEX: make_png(30, 12), ASY: make_png(200, 200)})
    report, failures, renderer = render(p, [problem])
    document = renderer.jobs[0].document
    assert report.rendered == 1 and failures == {}
    assert 'class="aops-tex' not in document and document.count("data:image/png;base64,") == 2
    assert LATEX_HOST not in document and "mathjax" not in document.lower()  # nothing left to typeset
    assert set(client.calls) == {LATEX, ASY}
    assert repo.conn.execute("SELECT COUNT(*) FROM problem_assets").fetchone()[0] == 2


def test_a_formula_png_that_is_gone_falls_back_to_its_tex(repo, tmp_path):
    problem = add_problem(repo, html=statement(), key="AMC10A", year=2024)
    p, _ = pipeline(repo, tmp_path, {LATEX: HttpStatusError("HTTP 404", url=LATEX, status=404), ASY: make_png()})
    report, failures, renderer = render(p, [problem])
    assert failures == {} and report.rendered == 1  # a missing formula image is not fatal
    document = renderer.jobs[0].document
    assert '<span class="aops-tex">\\(x^2\\)</span>' in document and "mathjax" in document.lower()
    assert document.count("data:image/png;base64,") == 1  # the diagram is still AoPS's PNG


def test_a_missing_diagram_fails_the_render_and_is_recorded(repo, tmp_path):
    problem = add_problem(repo, html=statement(), key="AMC10A", year=2024)
    p, _ = pipeline(repo, tmp_path, {LATEX: make_png(30, 12), ASY: HttpStatusError("HTTP 404", url=ASY, status=404)})
    report, failures, renderer = render(p, [problem])
    assert report.failed == 1 and renderer.jobs == [] and ASY in failures[problem.id]
    row = repo.conn.execute("SELECT status, error FROM renders WHERE problem_id = ?", (problem.id,)).fetchone()
    assert row["status"] == "failed" and row["error"].startswith("asset:") and "HTTP 404" in row["error"]


def test_a_challenged_wiki_image_stays_unavailable(repo, tmp_path):
    wiki = "https://artofproblemsolving.com/wiki/images/a/a1/x.png"
    blocked = SourceBlockedError("challenge", url=wiki, status=403)
    problem = add_problem(repo, html=f'<p>See <img src="{wiki}" alt="x"></p>', key="AMC10A", year=2024)
    p, _ = pipeline(repo, tmp_path, {wiki: blocked})
    report, failures, _ = render(p, [problem])
    assert report.failed == 1 and "challenge" in failures[problem.id]
    assert not repo.get_asset(wiki)["error"].startswith("temporary")  # permanent: retried after a day


@pytest.mark.parametrize(
    "error",
    [TransportError("timed out", url=LATEX), HttpStatusError("HTTP 503", url=LATEX, status=503)],
)
def test_a_temporary_formula_failure_waits_for_the_next_run_instead_of_using_tex(repo, tmp_path, error):
    """A TeX stand-in would never be replaced by the PNG later, so the problem is retried instead."""
    problem = add_problem(repo, html=statement(), key="AMC10A", year=2024)
    p, client = pipeline(repo, tmp_path, {LATEX: error, ASY: make_png()})
    report, failures, renderer = render(p, [problem])
    assert report.failed == 1 and renderer.jobs == [] and "temporary" in failures[problem.id]
    assert [p.id for p in p.plan()] == [problem.id]  # still needs rendering
    with pytest.raises(AssetError) as cached:
        p.assets.ensure(LATEX)
    assert cached.value.retryable and client.calls.count(LATEX) == 1  # remembered, not re-requested


def test_a_robots_refusal_is_retryable_and_never_cached(repo, tmp_path):
    refusal = RobotsDisallowedError("robots.txt at https://latex.artofproblemsolving.com disallows /a", url=LATEX)
    problem = add_problem(repo, html=statement(), key="AMC10A", year=2024)
    p, client = pipeline(repo, tmp_path, {LATEX: refusal, ASY: make_png()})
    report, failures, _ = render(p, [problem])
    assert report.failed == 1 and "robots.txt" in failures[problem.id]  # not rendered with TeX instead
    assert repo.get_asset(LATEX) is None  # the moment the host is listed, the image is fetched

    client.responses[LATEX] = make_png(30, 12)
    report, failures, _ = render(p, [problem])
    assert failures == {} and report.rendered == 1


def test_robots_refusals_stored_by_earlier_versions_are_ignored(repo, tmp_path):
    repo.upsert_asset(
        LATEX,
        status="failed",
        error="robots.txt at https://latex.artofproblemsolving.com disallows /a/b/c/formula.png",
        expires_at="2999-01-01T00:00:00Z",
    )
    p, client = pipeline(repo, tmp_path, {LATEX: make_png(30, 12)})
    assert p.assets.state(LATEX) == "missing"
    assert p.assets.ensure(LATEX)[1] == "image/png" and client.calls == [LATEX]
    assert p.assets.state(LATEX) == "cached"


def test_without_an_asset_cache_formulas_are_typeset(repo, tmp_path):
    problem = add_problem(repo, html=f'<p>Find <img class="latex" src="{LATEX}" alt="$x^2$">.</p>', key="AMC10A")
    renderer = FakeRenderer()
    asyncio.run(RenderPipeline(repo, SETTINGS, tmp_path / "renders").render([problem], renderer))
    assert '<span class="aops-tex">\\(x^2\\)</span>' in renderer.jobs[0].document


# --------------------------------------------------------------------------- prefetch-assets command


def test_prefetch_assets_counts_then_fetches_in_resumable_batches(tmp_path, monkeypatch, capsys):
    settings = load_settings({"DATA_DIR": str(tmp_path), "ROBOTS_OVERRIDE_HOSTS": LATEX_HOST})
    repo = Repository(open_database(settings.database_path))
    from mathbot.contests import ContestRegistry

    repo.sync_registry(ContestRegistry())
    add_problem(repo, html=statement(), key="AMC10A", year=2024)
    repo.conn.close()
    fake = AssetClient({LATEX: make_png(30, 12), ASY: make_png()})
    monkeypatch.setattr(cli, "_asset_client", lambda s: fake)

    def run(*, count=False, limit=None) -> str:
        assert cli.cmd_prefetch_assets(argparse.Namespace(contests=[], count=count, limit=limit), settings) == 0
        return capsys.readouterr().out

    out = run(count=True)
    assert "2 distinct image(s): 0 cached" in out and "2 to fetch" in out and fake.calls == []
    assert "not in ROBOTS_OVERRIDE_HOSTS" not in out
    assert "1 left for later runs" in run(limit=1) and fake.calls == [LATEX]
    assert "0 left for later runs" in run() and fake.calls == [LATEX, ASY]
    assert "2 cached" in run(count=True)  # a rerun resumes where the last one stopped
