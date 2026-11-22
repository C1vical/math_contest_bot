"""/problem, /random and the problem buttons, driven through a fake interaction (no Discord)."""

from __future__ import annotations

import asyncio
import types

import discord
import pytest

from mathbot.bot import ui
from mathbot.bot.cogs.problems import Problems
from mathbot.bot.query import RandomFilters
from mathbot.bot.services import Database
from mathbot.config import load_settings
from tests.conftest import add_problem


class FakeResponse:
    def __init__(self):
        self.done = False
        self.messages: list[dict] = []
        self.deferred = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **_kwargs):
        self.done = self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.messages: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, client, *, custom_id=None, channel_id=10, user_id=1, roles=()):
        self.client = client
        self.channel_id = channel_id
        self.channel = types.SimpleNamespace(parent_id=None)
        self.user = types.SimpleNamespace(
            id=user_id,
            roles=[types.SimpleNamespace(id=r) for r in roles],
            guild_permissions=types.SimpleNamespace(manage_guild=False),
        )
        self.type = discord.InteractionType.component if custom_id else discord.InteractionType.application_command
        self.data = {"custom_id": custom_id} if custom_id else {}
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.namespace = types.SimpleNamespace()

    @property
    def sent(self) -> list[dict]:
        return self.response.messages + self.followup.messages


class FakeImages:
    async def get(self, _problem):
        return None  # text fallback: no image service needed


@pytest.fixture
def bot(repo, registry, tmp_path):
    def make(**env):
        settings = load_settings({"DATA_DIR": str(tmp_path), "COMMAND_COOLDOWN_SECONDS": "0", **env})
        return types.SimpleNamespace(
            settings=settings,
            registry=registry,
            db=Database(tmp_path / "db" / "problems.db"),  # the repo fixture's database
            images=FakeImages(),
        )

    return make


def amc(repo, numbers=(1, 2, 3), year=2024):
    for n in numbers:
        add_problem(repo, n, f"<p>AMC 10A {year} problem {n}: what is {n} plus {n}?</p>", key="AMC10A", year=year)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- /problem


@pytest.mark.parametrize("text", ["AMC 10A 2024 #2", "amc10a-2024-p02", "2024 amc10a problem 2"])
def test_problem_accepts_everything_in_the_contest_option(repo, bot, text):
    amc(repo)
    client = bot()
    interaction = FakeInteraction(client)
    run(Problems.problem.callback(Problems(client), interaction, text))
    [message] = interaction.followup.messages
    assert message["embed"].title == "2024 AMC 10A Problem 2"


def test_problem_without_a_number_explains_the_format(repo, bot):
    amc(repo)
    interaction = FakeInteraction(bot())
    run(Problems.problem.callback(Problems(interaction.client), interaction, "AMC10A", 2024))
    [message] = interaction.response.messages
    assert message["ephemeral"] and message["embed"].title == "Which AMC 10A problem?"
    assert "problem number is missing" in message["embed"].description and "2024" in message["embed"].description


def test_problem_view_has_navigation_with_ends_disabled(repo, bot):
    amc(repo)
    interaction = FakeInteraction(bot())
    run(Problems.problem.callback(Problems(interaction.client), interaction, "AMC10A", 2024, 1))
    view = interaction.followup.messages[0]["view"]
    buttons = {item.label: item for item in view.children}
    assert buttons["Previous"].disabled and not buttons["Next"].disabled
    assert buttons["Next"].custom_id == ui.nav_custom_id("amc10a-2024-p01", +1)
    assert RandomFilters.from_custom_id(buttons["Another random"].custom_id) == RandomFilters(contest="AMC10A")


# --------------------------------------------------------------------------- /random


def test_random_does_not_repeat_recent_problems_in_a_channel(repo, bot):
    amc(repo, numbers=(1, 2, 3))
    client = bot()
    cog = Problems(client)
    titles = []
    for _ in range(3):
        interaction = FakeInteraction(client)
        run(Problems.random.callback(cog, interaction, "AMC10A"))
        titles.append(interaction.followup.messages[0]["embed"].title)
    assert len(set(titles)) == 3
    interaction = FakeInteraction(client)  # all three seen: a repeat beats "nothing found"
    run(Problems.random.callback(cog, interaction, "AMC10A"))
    assert interaction.followup.messages[0]["embed"].title in titles


def test_random_with_no_match_says_which_filters(repo, bot):
    amc(repo)
    interaction = FakeInteraction(bot())
    run(Problems.random.callback(Problems(interaction.client), interaction, "AMC10A", None, 2030))
    [message] = interaction.response.messages
    assert message["ephemeral"] and "years 2030 onward" in message["embed"].description


def test_random_unknown_contest_suggests_names(repo, bot):
    interaction = FakeInteraction(bot())
    run(Problems.random.callback(Problems(interaction.client), interaction, "amc10x"))
    assert "AMC10A" in interaction.response.messages[0]["embed"].description


# --------------------------------------------------------------------------- buttons


def test_next_button_posts_the_next_problem(repo, bot):
    amc(repo, numbers=(1, 2))
    client = bot()
    interaction = FakeInteraction(client, custom_id=ui.nav_custom_id("amc10a-2024-p01", +1))
    run(Problems(client).on_interaction(interaction))
    assert interaction.response.deferred
    assert interaction.followup.messages[0]["embed"].title == "2024 AMC 10A Problem 2"


def test_next_button_at_the_end_explains_privately(repo, bot):
    amc(repo, numbers=(1,))
    client = bot()
    interaction = FakeInteraction(client, custom_id=ui.nav_custom_id("amc10a-2024-p01", +1))
    run(Problems(client).on_interaction(interaction))
    [message] = interaction.response.messages
    assert message["ephemeral"] and "no next problem" in message["content"]


def test_another_random_button_repeats_the_filters(repo, bot):
    amc(repo, numbers=(1, 2), year=2020)
    add_problem(repo, 1, "<p>An IMO problem that must not be picked.</p>", key="IMO", year=2020)
    client = bot()
    custom_id = RandomFilters(contest="AMC10A", year=2020).custom_id()
    interaction = FakeInteraction(client, custom_id=custom_id)
    run(Problems(client).on_interaction(interaction))
    assert interaction.followup.messages[0]["embed"].title.startswith("2020 AMC 10A")


def test_buttons_obey_the_channel_allow_list(repo, bot):
    amc(repo)
    client = bot(ALLOWED_CHANNEL_IDS="99")
    interaction = FakeInteraction(client, custom_id=ui.nav_custom_id("amc10a-2024-p01", +1), channel_id=10)
    run(Problems(client).on_interaction(interaction))
    assert interaction.followup.messages == [] and "can't be used here" in interaction.response.messages[0]["content"]


def test_buttons_have_a_per_user_cooldown(repo, bot):
    amc(repo)
    client = bot(COMMAND_COOLDOWN_SECONDS="30")
    cog = Problems(client)
    custom_id = ui.nav_custom_id("amc10a-2024-p01", +1)
    first, second = FakeInteraction(client, custom_id=custom_id), FakeInteraction(client, custom_id=custom_id)
    run(cog.on_interaction(first))
    run(cog.on_interaction(second))
    assert first.followup.messages and "Slow down" in second.response.messages[0]["content"]


def test_foreign_and_malformed_button_ids_are_ignored(repo, bot):
    client = bot()
    for custom_id in ("some-other-view:next", "mathbot:nav:+5:x", "mathbot:rand:zz=1"):
        interaction = FakeInteraction(client, custom_id=custom_id)
        run(Problems(client).on_interaction(interaction))
        assert interaction.sent == []


def test_help_lists_commands_and_the_buttons_topic_within_embed_limits(bot):
    from mathbot.bot.cogs.general import HELP, General

    interaction = FakeInteraction(bot())
    run(General.help.callback(General(interaction.client), interaction))
    embed = interaction.response.messages[0]["embed"]
    listing = embed.fields[0].value
    assert "`/random`" in listing and "`Buttons`" in listing and "`/admin`" in listing
    assert len(listing) <= 1024 and all(len(details) <= 4096 for _, details in HELP.values())
    detail = FakeInteraction(interaction.client)
    run(General.help.callback(General(interaction.client), detail, "buttons"))
    assert detail.response.messages[0]["embed"].title.startswith("Buttons")


def test_every_command_fits_discord_limits(tmp_path, registry):
    from mathbot.bot.app import MathBot
    from mathbot.bot.health import HealthState

    settings = load_settings({"DATA_DIR": str(tmp_path), "SYNC_COMMANDS": "false"})

    async def payloads():
        bot = MathBot(settings, registry, HealthState())
        await bot.setup_hook()
        data = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
        await bot.close()
        return data

    def check(command):
        assert 1 <= len(command["description"]) <= 100, command["name"]
        options = command.get("options", [])
        assert len(options) <= 25
        for option in options:
            assert len(option["description"]) <= 100, (command["name"], option["name"])
            assert all(len(choice["name"]) <= 100 for choice in option.get("choices", []))
            if option["type"] in (1, 2):  # subcommand / group
                check(option)

    for command in run(payloads()):
        check(command)


def _bot_with_fake_discord(tmp_path, registry, **env):
    from mathbot.bot.app import MathBot
    from mathbot.bot.health import HealthState

    settings = load_settings({"DATA_DIR": str(tmp_path), "SYNC_COMMANDS": "false", **env})
    bot = MathBot(settings, registry, HealthState())
    calls: list[tuple] = []

    async def tree_sync(*, guild=None):
        calls.append(("sync", guild.id if guild else None))
        return ["cmd"] * 7

    async def bulk_upsert_global_commands(application_id, payload):
        calls.append(("global", application_id, list(payload)))
        return []

    bot.tree.sync = tree_sync
    bot.http.bulk_upsert_global_commands = bulk_upsert_global_commands
    bot._connection.application_id = 42
    return bot, calls


def test_guild_sync_also_removes_stale_global_commands(tmp_path, registry):
    """With GUILD_ID set, commands live in that server. Global ones registered by an older
    version (/gimme, /info, a second /problem) would otherwise stay forever."""
    bot, calls = _bot_with_fake_discord(tmp_path, registry, GUILD_ID="123")

    async def go():
        await bot.setup_hook()
        count = await bot.sync_commands()
        await bot.close()
        return count

    assert run(go()) == 7
    assert calls == [("sync", 123), ("global", 42, [])]


def test_global_sync_leaves_global_commands_to_the_tree(tmp_path, registry):
    bot, calls = _bot_with_fake_discord(tmp_path, registry)

    async def go():
        await bot.setup_hook()
        await bot.sync_commands()
        await bot.close()

    run(go())
    assert calls == [("sync", None)]
