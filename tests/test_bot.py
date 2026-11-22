"""Bot logic that can be exercised without connecting to Discord."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from mathbot.bot import ui
from mathbot.bot.app import MathBot
from mathbot.bot.cogs.contests import contest_pages
from mathbot.bot.health import HealthServer, HealthState, create_health_app
from mathbot.bot.images import ImageFetchError, ImageService, validate_image_response
from mathbot.bot.policy import is_admin, is_allowed
from mathbot.bot.services import Database
from mathbot.config import BotSettings, load_settings
from mathbot.contests import compress_years
from mathbot.db.repo import SeriesStats
from mathbot.render.pipeline import RenderPipeline
from tests.conftest import FakeRenderer, add_problem, make_png

# --------------------------------------------------------------------------- policy


def test_channel_and_role_policy():
    anywhere = BotSettings()
    assert is_allowed(anywhere, [1], [])
    channel_only = BotSettings(allowed_channel_ids=frozenset({10}))
    assert is_allowed(channel_only, [10], []) and not is_allowed(channel_only, [11], [])
    assert is_allowed(channel_only, [99, 10], [])  # a thread whose parent channel is allowed
    both = BotSettings(allowed_channel_ids=frozenset({10}), allowed_role_ids=frozenset({5}))
    assert is_allowed(both, [10], [5, 6]) and not is_allowed(both, [10], [6]) and not is_allowed(both, [11], [5])


def test_admin_policy():
    default = BotSettings()
    assert is_admin(default, 1, [], manage_guild=True) and not is_admin(default, 1, [], manage_guild=False)
    configured = BotSettings(admin_role_ids=frozenset({7}), admin_user_ids=frozenset({42}))
    assert is_admin(configured, 42, [], manage_guild=False)
    assert is_admin(configured, 1, [7], manage_guild=False)
    assert not is_admin(configured, 1, [8], manage_guild=True)  # explicit lists replace the permission fallback


# --------------------------------------------------------------------------- ui helpers


def test_compress_years():
    assert compress_years([2016, 2017, 2018, 2020, 2022, 2023]) == "2016-2018, 2020, 2022-2023"
    assert compress_years([]) == "none" and compress_years([1999]) == "1999"


def test_contest_autocomplete_is_case_insensitive_and_capped(registry):
    values = [c.value for c in ui.contest_choices(registry, "amc 10")]
    assert values[0] == "AMC10" and {"AMC10A", "AMC10B", "AMC10FALLA"} <= set(values)
    assert [c.value for c in ui.contest_choices(registry, "aime ii")] == ["AIME2"]
    assert [c.value for c in ui.contest_choices(registry, "repechage")] == []  # CMOQR is disabled
    assert len(ui.contest_choices(registry, "")) <= 25
    with_families = [c.value for c in ui.contest_choices(registry, "pcf", families=True)]
    assert with_families == ["PCF"]
    assert all(len(c.name) <= 100 for c in ui.contest_choices(registry, "", families=True))


def test_suggestions_for_misspelled_contests(registry):
    assert "EUCLID" in ui.suggest_contests(registry, "euclidd")
    assert "AMC10A" in ui.suggest_contests(registry, "amc10x")
    assert ui.suggest_contests(registry, "zzzzzz") == []


def test_problem_embed_with_and_without_image(repo):
    problem = add_problem(repo, html="<p>What is the value of 6 times 7?</p>")
    with_image = ui.problem_embed(problem, has_image=True)
    assert with_image.title == "2020 IMO Problem 1" and with_image.image.url == "attachment://problem.png"
    assert with_image.footer.text == "Source: AoPS Wiki"
    fallback = ui.problem_embed(problem, has_image=False)
    assert "6 times 7" in fallback.description and fallback.fields[0].name == "Image unavailable"
    # A database newer than the code (e.g. a published snapshot) may hold a series this
    # registry doesn't know; showing its problems must not depend on a registry lookup.
    newer = dataclasses.replace(problem, series_key="NEWCONTEST", provider="cemc")
    assert ui.problem_embed(newer, has_image=False).footer.text == "Source: CEMC (University of Waterloo)"


def test_unknown_contest_reply_suggests_close_names(registry):
    embed = ui.unknown_contest_embed(registry, "euclidd")
    assert embed.title == "Unknown contest" and "`EUCLID`" in embed.description
    assert "See `/contests`" in ui.unknown_contest_embed(registry, "zzzzzz").description


def test_answer_button_only_when_an_answer_exists(repo):
    problem = add_problem(repo)

    async def build():
        plain = ui.problem_view(problem, owner_id=1)
        answered = ui.problem_view(dataclasses.replace(problem, answer="C"), owner_id=1)
        return [i.label for i in plain.children], [(i.label, i.custom_id) for i in answered.children]

    plain, answered = asyncio.run(build())
    assert "Show answer" not in plain
    assert ("Show answer", f"{ui.ANSWER_PREFIX}{problem.id}") in answered


def test_long_contest_lists_are_paginated_within_embed_limits():
    stats = [
        SeriesStats(f"KEY{i}", f"Contest {i}", f"FAMILY{i % 9}", "aops_wiki", 3, 75 if i % 2 else 0, 2000, 2002)
        for i in range(40)
    ]
    pages = contest_pages(stats)
    assert len(pages) == 3
    assert all(len(page.fields) <= 4 and all(len(f.value) <= 1024 for f in page.fields) for page in pages)
    assert "*(not loaded)*" in pages[0].fields[0].value
    assert contest_pages([])[0].description == "No contests are enabled."


# --------------------------------------------------------------------------- image delivery


@pytest.mark.parametrize(
    "status, content_type, data, message",
    [
        (404, "image/png", make_png(), "HTTP 404"),
        (200, "text/html; charset=utf-8", b"<html>Not Found</html>", "content type"),
        (200, "image/png", b"<html>masquerading</html>", "not a PNG"),
        (200, "image/png", make_png() + b"\0" * (9 * 1024 * 1024), "too large"),
    ],
)
def test_remote_image_responses_are_validated(status, content_type, data, message):
    with pytest.raises(ImageFetchError, match=message):
        validate_image_response(status, content_type, data)


def test_valid_remote_image_passes():
    validate_image_response(200, "image/png", make_png())


def image_service(tmp_path, **env) -> ImageService:
    settings = load_settings({"DATA_DIR": str(tmp_path), "DATABASE_PATH": str(tmp_path / "db" / "problems.db"), **env})
    return ImageService(settings, Database(settings.database_path))


def test_local_render_is_served_without_any_network(repo, tmp_path):
    problem = add_problem(repo)
    pipeline = RenderPipeline(repo, load_settings({}).render, tmp_path / "renders")
    asyncio.run(pipeline.render([problem], FakeRenderer()))
    service = image_service(tmp_path)

    async def go():
        first = await service.get(problem)
        second = await service.get(problem)  # served from the in-memory cache
        await service.close()
        return first, second

    first, second = asyncio.run(go())
    assert first is not None and first.startswith(b"\x89PNG") and first is second


def test_missing_render_and_no_remote_yields_none(repo, tmp_path):
    problem = add_problem(repo)
    service = image_service(tmp_path)
    assert asyncio.run(service.get(problem)) is None


def test_remote_lookup_uses_the_current_render_key(repo, tmp_path):
    problem = add_problem(repo)
    pipeline = RenderPipeline(repo, load_settings({}).render, tmp_path / "elsewhere")  # file not on this disk
    asyncio.run(pipeline.render([problem], FakeRenderer()))
    service = image_service(tmp_path, IMAGE_BASE_URL="https://img.example.org", S3_KEY_PREFIX="renders")
    attempts: list[str] = []

    async def fake_fetch(url):
        attempts.append(url)
        return make_png()

    service._fetch = fake_fetch  # type: ignore[method-assign]
    assert asyncio.run(service.get(problem)) is not None
    assert len(attempts) == 1 and attempts[0].startswith("https://img.example.org/renders/imo/imo-2020-p01-")

    async def failing_fetch(_url):
        raise ImageFetchError("HTTP 404")

    service = image_service(tmp_path, IMAGE_BASE_URL="https://img.example.org")
    service._fetch = failing_fetch  # type: ignore[method-assign]
    assert asyncio.run(service.get(problem)) is None  # no fallback to old object names


# --------------------------------------------------------------------------- health endpoint


def test_health_endpoint_reflects_readiness_and_database():
    state = HealthState(db_check=lambda: True)
    client = create_health_app(state).test_client()
    assert client.get("/").data == b"Bot is alive!"
    starting = client.get("/healthz")
    assert starting.status_code == 503 and starting.get_json()["status"] == "starting"
    state.ready = True
    assert client.get("/healthz").status_code == 200
    state.db_check = lambda: False
    degraded = client.get("/healthz")
    assert degraded.status_code == 503 and degraded.get_json() == {
        "status": "degraded",
        "discord_ready": True,
        "database": False,
        "uptime_seconds": degraded.get_json()["uptime_seconds"],
    }


def test_health_server_starts_and_stops_cleanly():
    import urllib.request

    server = HealthServer(HealthState(), "127.0.0.1", 0)  # port 0: pick a free port
    server.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=5) as response:
            assert response.read() == b"Bot is alive!"
    finally:
        server.stop()
    assert server._thread is None
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=2)


def test_database_ping(repo, tmp_path):  # the repo fixture creates the database file
    assert Database(tmp_path / "db" / "problems.db").ping() is True


# --------------------------------------------------------------------------- command tree


def test_bot_loads_all_cogs_and_registers_expected_commands(tmp_path, registry):
    settings = load_settings({"DATA_DIR": str(tmp_path), "SYNC_COMMANDS": "false"})

    async def go():
        bot = MathBot(settings, registry, HealthState())
        await bot.setup_hook()
        names = {command.qualified_name for command in bot.tree.walk_commands()}
        await bot.close()
        return names

    names = asyncio.run(go())
    assert {
        "problem",
        "random",
        "contests",
        "contest",
        "stats",
        "help",
        "admin",
        "admin status",
        "admin sync",
    } <= names
    assert not {"gimme", "info"} & names  # pre-2.0 aliases are gone


def test_cooldown_policy():
    from mathbot.bot.policy import cooldown_for

    assert cooldown_for(BotSettings(), 1).per == 3.0
    assert cooldown_for(BotSettings(command_cooldown=0), 1) is None
    assert cooldown_for(BotSettings(admin_user_ids=frozenset({7})), 7) is None


def test_every_kind_of_admin_is_exempt_from_the_cooldown():
    """COMMAND_COOLDOWN_SECONDS documents "admins exempt": admin roles and the Manage Server
    fallback count too, not only ADMIN_USER_IDS."""
    from mathbot.bot.policy import cooldown_for

    by_role = BotSettings(admin_role_ids=frozenset({5}))
    assert cooldown_for(by_role, 1, [5]) is None
    assert cooldown_for(by_role, 1, [6], manage_guild=True) is not None  # explicit lists replace the fallback
    assert cooldown_for(BotSettings(), 1, [], manage_guild=True) is None  # no lists: Manage Server is admin
    assert cooldown_for(BotSettings(), 1, [], manage_guild=False) is not None


def test_bot_does_not_import_the_render_pipeline_or_playwright():
    """The bot host needs neither Chromium nor the render stack (checked in a fresh interpreter)."""
    import subprocess
    import sys

    from mathbot.config import PROJECT_ROOT

    code = (
        "import sys, mathbot.bot.app\n"
        "from mathbot.bot.app import COGS\n"
        "import importlib\n"
        "[importlib.import_module(c) for c in COGS]\n"
        "print(sorted(m for m in sys.modules if m.startswith(('playwright', 'mathbot.render.pipeline', "
        "'mathbot.render.assets', 'mathbot.cli', 'mathbot.sources'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120, check=True
    )
    assert result.stdout.strip() == "[]"


def test_official_solution_link_appears_when_stored(repo):
    problem = dataclasses.replace(add_problem(repo), solution_url="https://cemc.uwaterloo.ca/solutions.html")

    async def build():
        return [(i.label, i.url) for i in ui.problem_view(problem, owner_id=1).children]

    assert ("Solution", "https://cemc.uwaterloo.ca/solutions.html") in asyncio.run(build())


def test_a_truncated_png_is_rejected():
    png = make_png()
    with pytest.raises(ImageFetchError, match="incomplete"):
        validate_image_response(200, "image/png", png[: len(png) // 2])


class _ChunkedStream:
    """aiohttp's StreamReader as far as the image fetch uses it: data arrives in pieces."""

    def __init__(self, data: bytes, size: int = 1000):
        self.chunks = [data[i : i + size] for i in range(0, len(data), size)]

    async def read(self, _n=-1):  # what the old code called: returns only what has arrived
        return self.chunks[0] if self.chunks else b""

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


def test_remote_images_are_read_to_the_end_not_just_the_first_chunk():
    from mathbot.bot.images import read_body

    png = make_png(800, 600)
    assert asyncio.run(read_body(_ChunkedStream(png), limit=len(png))) == png
    with pytest.raises(ImageFetchError, match="too large"):
        asyncio.run(read_body(_ChunkedStream(png), limit=len(png) - 1))
