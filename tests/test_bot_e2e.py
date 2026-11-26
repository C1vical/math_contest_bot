"""Every command and button through the real bot, against a Discord that enforces its limits."""

from __future__ import annotations

import asyncio

import pytest

from mathbot.bot import ui
from mathbot.bot.query import RandomFilters, parse_problem_reference
from mathbot.config import load_settings
from mathbot.contests import ContestRegistry
from tests.conftest import add_problem
from tests.discord_sim import ROLE, discord_sim

ADMIN = 99
OPTIONAL = "CJMO,CMOQR,GAUSS7,GAUSS8"


@pytest.fixture
def drive(repo, registry, tmp_path, monkeypatch):
    """``drive(scenario, ENV=...)`` runs ``await scenario(sim)`` against a small database."""
    for number in (1, 2, 3):
        add_problem(repo, number, f"<p>What is {number} plus {number}, exactly?</p>", key="AMC10A", year=2024)
    add_problem(repo, 1, "<p>What is seven times eight, exactly?</p>", key="CIMC", year=2019)
    add_problem(repo, 1, "<p>What is nine minus four, exactly?</p>", key="AIME", year=1990)
    add_problem(repo, 1, "<p>What is nine minus five, exactly?</p>", key="AIME1", year=2019)
    repo.conn.execute("UPDATE problems SET answer = 'C' WHERE id = 'amc10a-2024-p01'")

    def run(scenario, *, contests=registry, **env):
        settings = load_settings(
            {
                "DATA_DIR": str(tmp_path),
                "DATABASE_PATH": str(tmp_path / "db" / "problems.db"),
                "SYNC_COMMANDS": "false",
                "COMMAND_COOLDOWN_SECONDS": "0",
                "ADMIN_USER_IDS": str(ADMIN),
                **env,
            }
        )

        async def go():
            async with discord_sim(settings, contests, monkeypatch) as sim:
                return await scenario(sim)

        return asyncio.run(go())

    return run


# --------------------------------------------------------------------------- commands


def test_every_command_gets_one_valid_public_reply(drive):
    async def scenario(sim):
        return {
            "problem": await sim.command("problem", contest="AMC10A", year=2024, number=1),
            "problem in one box": await sim.command("problem", contest="AMC 10A 2024 #1"),
            "random": await sim.command("random"),
            "random filtered": await sim.command("random", contest="CSIMC", year_from=2030, year_to=2000),
            "contests": await sim.command("contests"),
            "contest": await sim.command("contest", contest="CIMC"),
            "stats": await sim.command("stats"),
            "help": await sim.command("help"),
            "help topic": await sim.command("help", command="buttons"),
        }

    for name, reply in drive(scenario).items():
        assert reply.ok and not reply.ephemeral, (name, reply)


def test_admin_commands(drive):
    async def scenario(sim):
        async def sync():
            return 7

        sim.bot.sync_commands = sync
        allowed = [await sim.command(name, user=ADMIN) for name in ("admin status", "admin sync")]
        return allowed, [await sim.command(name) for name in ("admin status", "admin sync")]

    (status, sync), refused = drive(scenario)
    assert status.ok and status.ephemeral and "0 of 6 problems have an image" in status.text
    assert sync.ok and sync.ephemeral and "Synced 7" in sync.text
    assert all(r.ok and r.ephemeral and "administrators only" in r.text for r in refused)


def test_admin_status_does_not_need_the_rendered_files_on_this_disk(drive, repo):
    """The bot host has no renders directory: the old full check reported every problem as broken."""
    for row in repo.conn.execute("SELECT id, content_hash FROM problems").fetchall():
        repo.record_render(
            problem_id=row["id"],
            content_hash=row["content_hash"],
            renderer_version="7-w1000-s2",
            status="ok",
            local_path="not/on/this/disk.png",
        )

    async def scenario(sim):
        return await sim.command("admin status", user=ADMIN)

    assert "6 of 6 problems have an image" in drive(scenario).text


@pytest.mark.parametrize("text", ["x" * 5900, "`@everyone` **x** ||y||", "数学竞赛", "#", "-", "AMC 10A 2024 ²", "²"])
def test_strange_input_is_answered_privately(drive, text):
    async def scenario(sim):
        return [
            await sim.command("problem", contest=text),
            await sim.command("contest", contest=text),
            await sim.command("random", contest=text),
            await sim.command("help", command=text),
        ]

    for reply in drive(scenario):
        assert reply.ok and reply.ephemeral, reply
        assert len(reply.text) < 600


def test_digits_of_other_scripts_are_read_and_superscripts_are_not(registry):
    def resolves(name):
        return registry.resolve(name) is not None

    assert parse_problem_reference("AMC 10A ２０１９ #５", resolves).year == 2019
    assert parse_problem_reference("AMC 10A 2019 ²", resolves).number is None


def test_a_group_where_one_contest_is_needed_lists_its_members(drive):
    async def scenario(sim):
        return [await sim.command("problem", contest="csimc"), await sim.command("contest", contest="PCF")]

    problem, contest = drive(scenario)
    assert problem.title == contest.title == "Pick one contest"
    assert "`CIMC`, `CSMC`" in problem.text and "`PASCAL`, `CAYLEY`, `FERMAT`" in contest.text


def test_a_year_after_a_contest_was_split_points_to_its_successors(drive):
    async def scenario(sim):
        return [
            await sim.command("problem", contest="AIME 2019 #1"),
            await sim.command("problem", contest="CIMC 1950 #1"),
        ]

    split, plain = drive(scenario)
    assert "For 2019, try `AIME1`." in split.text
    assert plain.title == "Year not available" and "try" not in plain.text


def test_a_command_from_an_older_version_is_explained(drive):
    async def scenario(sim):
        return await sim.command("gimme"), await sim.autocomplete("gimme", "contest", "a")

    used, typed = drive(scenario)
    assert used.ok and used.ephemeral and "no longer exists" in used.text
    # A message cannot answer an autocomplete: trying to send one was logged as an error each time.
    assert not typed.rejected and not typed.errors and not typed.messages


def test_commands_work_in_direct_messages(drive):
    async def scenario(sim):
        return [await sim.run("random", {}, dm=True), await sim.press("mathbot:nav:+1:1:amc10a-2024-p01", dm=True)]

    assert all(reply.ok for reply in drive(scenario))


def test_commands_work_in_group_dms(drive):
    """Bots cannot join group DMs; the commands arrive from a user's own install of the app.
    Replies are public there, and the buttons and the contest list's pages still work."""

    async def scenario(sim):
        random = await sim.run("random", {}, group_dm=True)
        nav = await sim.press("mathbot:nav:+1:1:amc10a-2024-p01", group_dm=True)
        answer = await sim.press(f"{ui.ANSWER_PREFIX}amc10a-2024-p01", user=2, group_dm=True)
        listing = await sim.run("contests", {}, group_dm=True)
        return random, nav, answer, listing

    random, nav, answer, listing = drive(scenario)
    assert all(reply.ok for reply in (random, nav, answer, listing))
    assert not random.ephemeral and not nav.ephemeral and not listing.messages[0].get("flags", 0) & 64
    assert answer.ephemeral and "||C||" in answer.text


def test_slow_counting_commands_are_acknowledged_before_the_database_is_read(drive):
    async def scenario(sim):
        order = []
        call = sim.bot.db.call

        async def watched(fn):
            order.append(sim.current.deferred)
            return await call(fn)

        sim.bot.db.call = watched
        for name, options in (("stats", {}), ("contests", {}), ("contest", {"contest": "CIMC"}), ("admin status", {})):
            await sim.run(name, options, user=ADMIN)
        return order

    assert drive(scenario) == [True] * 4


# --------------------------------------------------------------------------- autocomplete


def values(reply):
    assert reply.ok, reply
    found = [choice["value"] for choice in reply.choices]
    assert len(found) == len(set(found)), found  # two entries that do the same thing
    return found


@pytest.mark.parametrize("command", ["problem", "contest", "random"])
@pytest.mark.parametrize("enable", ["", OPTIONAL])
def test_contests_still_held_are_offered_before_typing(drive, command, enable):
    """29 contests and 25 entries: CIMC, CSMC, Cayley and Fermat used to be the ones cut off."""
    contests = ContestRegistry(enable=filter(None, enable.split(",")))

    async def scenario(sim):
        return await sim.autocomplete(command, "contest", "")

    offered = values(drive(scenario, contests=contests))
    held = {s.key for s in contests.all() if s.eras[-1].last_year is None}
    if command != "random":
        assert held <= set(offered)
    elif not enable:  # groups come first in /random; every contest is in the list or in a listed group
        assert held <= set(offered)
    else:
        assert {s.key for s in contests.all() if s.family in offered} | set(offered) >= held


@pytest.mark.parametrize("command", ["problem", "contest", "random"])
def test_every_contest_is_found_by_typing_its_name(drive, command):
    contests = ContestRegistry(enable=OPTIONAL.split(","))

    async def scenario(sim):
        return [
            (series, text, await sim.autocomplete(command, "contest", text))
            for series in contests.all()
            for text in {series.key, series.key.lower(), series.name, series.key[:3]}
        ]

    for series, text, reply in drive(scenario, contests=contests):
        assert series.key in values(reply), text


def test_contests_are_found_by_family_and_source(drive):
    async def scenario(sim):
        return [await sim.autocomplete("problem", "contest", text) for text in ("csimc", "cemc", "waterloo", "ci")]

    family, source, place, short = (values(r) for r in drive(scenario))
    assert family == ["CIMC", "CSMC"]
    assert source == place and {"CIMC", "CSMC", "EUCLID", "PASCAL"} <= set(source)
    assert short == ["CIMC"]  # too short to be looked up in family names


def test_a_whole_reference_is_offered_as_one_choice(drive):
    async def scenario(sim):
        full = await sim.autocomplete("problem", "contest", "cimc 2019 #1")
        year = await sim.autocomplete("problem", "contest", "cimc 2019")
        picked = await sim.command("problem", contest=full.choices[0]["value"])
        years = await sim.autocomplete("problem", "year", "", contest="AMC 10A 2024")
        return full, year, picked, years

    full, year, picked, years = drive(scenario)
    assert [c["name"] for c in full.choices] == ["2019 CIMC Problem 1"]
    assert [c["name"] for c in year.choices] == ["2019 CIMC"]
    assert picked.ok and picked.title == "2019 CIMC Problem 1"
    assert values(years) == [2024]


# --------------------------------------------------------------------------- buttons


def test_every_button_under_a_problem(drive):
    async def scenario(sim):
        first = await sim.command("problem", contest="AMC10A", year=2024, number=1)
        second = await sim.press(first.button("Next")["custom_id"])
        back = await sim.press(second.button("Previous")["custom_id"])
        answer = await sim.press(first.button("Show answer")["custom_id"], user=2)
        rolled = await sim.command("random", contest="CIMC")
        again = await sim.press(rolled.button("Another random")["custom_id"])
        return first, second, back, answer, rolled, again

    first, second, back, answer, rolled, again = drive(scenario)
    assert all(r.ok for r in (first, second, back, answer, rolled, again))
    assert first.button("Previous")["disabled"] and not first.button("Next")["disabled"]
    assert second.title == "2024 AMC 10A Problem 2" and back.title == first.title == "2024 AMC 10A Problem 1"
    assert answer.ephemeral and "||C||" in answer.text
    assert again.title == rolled.title == "2019 CIMC Problem 1" and "Another random" in again.buttons
    assert first.button("Another random") is None and first.button("Source")["url"].startswith("https://")


def test_buttons_refuse_other_people_and_the_end_of_the_paper(drive):
    async def scenario(sim):
        stranger = [await sim.press(i, user=2) for i in ("mathbot:nav:+1:1:amc10a-2024-p01", "mathbot:rand:u=1")]
        return stranger, await sim.press("mathbot:nav:+1:1:amc10a-2024-p03")

    stranger, end = drive(scenario)
    assert all(r.ok and r.ephemeral and "Only <@1>" in r.text for r in stranger)
    assert end.ok and end.ephemeral and "no next problem" in end.text


@pytest.mark.parametrize("rule", [{"ALLOWED_CHANNEL_IDS": "999"}, {"ALLOWED_ROLE_IDS": str(ROLE)}])
def test_commands_and_all_buttons_obey_the_channel_and_role_rules(drive, rule):
    async def scenario(sim):
        where = {"channel": 999, "roles": (ROLE,)}
        ids = ("mathbot:nav:+1:1:amc10a-2024-p01", "mathbot:rand:u=1", "mathbot:answer:amc10a-2024-p01")
        refused = [await sim.command("random"), *[await sim.press(i) for i in ids]]
        allowed = [await sim.run("random", {}, **where), *[await sim.press(i, **where) for i in ids]]
        return refused, allowed

    refused, allowed = drive(scenario, **rule)
    assert all(r.ok and r.ephemeral and "can't be used here" in r.text for r in refused)
    assert all(r.ok and "can't be used here" not in r.text for r in allowed)


def test_a_button_that_fails_tells_the_user(drive):
    """Errors in a listener only reach the log, so the press ended in "This interaction failed"."""

    async def scenario(sim):
        async def broken(_fn):
            raise RuntimeError("database is gone")

        sim.bot.db.call = broken
        ids = ("mathbot:nav:+1:1:amc10a-2024-p01", "mathbot:rand:u=1", "mathbot:answer:amc10a-2024-p01")
        return [await sim.press(i) for i in ids]

    for reply in drive(scenario):
        assert reply.ephemeral and "mention reference" in reply.text
        assert not reply.rejected and len(reply.errors) == 1 and "database is gone" in reply.errors[0]


@pytest.mark.parametrize(
    "custom_id",
    [
        "mathbot:rand:y=99999999999999999999",  # overflowed SQLite's integers
        "mathbot:rand:n=" + "9" * 30,
        "mathbot:rand:n=0",
        "mathbot:rand:u=" + "9" * 30,
        "mathbot:nav:+1:²:amc10a-2024-p01",
        "mathbot:nav:+2:1:amc10a-2024-p01",
    ],
)
def test_tampered_button_ids_are_ignored_without_an_error(drive, custom_id):
    async def scenario(sim):
        return await sim.press(custom_id)

    reply = drive(scenario)
    assert not reply.messages and not reply.errors and not reply.deferred


def test_the_longest_filters_and_user_id_still_fit_a_button(registry):
    longest = max((s.key for s in registry.all(include_disabled=True)), key=len)
    filters = RandomFilters(longest, 2000, 2000, 2026, 25, "medium", "aops_community", 2**63)
    assert filters.custom_id() is not None
    assert RandomFilters.from_custom_id(filters.custom_id()) == filters


def test_problem_buttons_are_not_kept_in_memory(drive):
    """They are answered by a listener; discord.py stored a view for every problem ever shown."""

    async def scenario(sim):
        for _ in range(5):
            await sim.command("random")
        return len(sim.bot._connection._view_store._views), len(sim.bot.persistent_views)

    assert drive(scenario) == (0, 0)


def test_contest_list_pages(drive):
    async def scenario(sim):
        first = await sim.command("contests")
        where = {"message_id": first.message_id}
        following = await sim.press(first.button("Next")["custom_id"], **where)
        stranger = await sim.press(first.button("Next")["custom_id"], user=2, **where)
        view = next(iter(sim.bot._connection._view_store._synced_message_views.values()))
        await view.on_timeout()
        return first, following, stranger, list(sim.current.edits)

    first, following, stranger, expired = drive(scenario)
    assert first.ok and following.ok and stranger.ok and stranger.ephemeral
    assert first.button("Previous")["disabled"]
    assert following.edits[0]["embeds"][0]["footer"]["text"].startswith("Page 2/")
    assert all(b["disabled"] for row in expired[-1]["components"] for b in row["components"])


def test_clip_makes_one_short_line():
    assert ui.clip("a\n  `b`") == "a 'b'" and ui.clip("   ") == "(blank)"
    assert len(ui.clip("x" * 5000)) == ui.MAX_ECHO_CHARS
