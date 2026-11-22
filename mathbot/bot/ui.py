"""Embeds, views and autocomplete shared by the cogs."""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import io

import discord
from discord import app_commands

from mathbot.bot.query import RandomFilters
from mathbot.contests import FAMILY_NAMES, PROVIDER_NAMES, ContestRegistry, normalize_alias
from mathbot.db.repo import ProblemRow

COLOR = discord.Color.blue()
ERROR_COLOR = discord.Color.red()
ANSWER_PREFIX = "mathbot:answer:"
NAV_PREFIX = "mathbot:nav:"  # mathbot:nav:<+1|-1>:<problem id>
MAX_CHOICES = 25  # Discord's autocomplete limit
MAX_LABEL_CHARS = 100  # Discord's limit for an autocomplete entry's name
MAX_STATEMENT_CHARS = 3900  # text fallback, under Discord's 4096-character embed description limit


def seconds(value: float) -> str:
    """A wait for humans: ``0.4s``, ``3s`` (never "0s")."""
    return f"{value:.1f}s" if value < 10 else f"{value:.0f}s"


def error_embed(title: str, description: str) -> discord.Embed:
    """A red embed for a reply that explains what went wrong."""
    return discord.Embed(title=title, description=description, color=ERROR_COLOR)


def suggest_contests(registry: ContestRegistry, text: str, limit: int = 5) -> list[str]:
    """Closest contest keys to a name that did not resolve."""
    aliases = registry.aliases()
    enabled = {s.key for s in registry.all()}
    close = difflib.get_close_matches(normalize_alias(text), list(aliases), n=limit * 2, cutoff=0.5)
    keys = list(dict.fromkeys(aliases[alias] for alias in close if aliases[alias] in enabled))
    return keys[:limit]


def unknown_contest_embed(registry: ContestRegistry, text: str) -> discord.Embed:
    """The reply to a contest name that did not resolve, with the closest matches."""
    suggestions = suggest_contests(registry, text)
    hint = f"Did you mean {', '.join(f'`{k}`' for k in suggestions)}?" if suggestions else "See `/contests`."
    return error_embed("Unknown contest", f"I don't know a contest called `{text}`. {hint}")


def contest_choices(
    registry: ContestRegistry, current: str, *, families: bool = False
) -> list[app_commands.Choice[str]]:
    """Autocomplete entries matching ``current`` against keys, names and aliases."""
    needle = normalize_alias(current)
    choices: list[app_commands.Choice[str]] = []
    if families:
        for family, members in registry.families().items():
            if len(members) > 1 and (not needle or needle in normalize_alias(family)):
                label = f"{family} - all of {FAMILY_NAMES.get(family, family)}"
                choices.append(app_commands.Choice(name=label[:MAX_LABEL_CHARS], value=family))
    for series in registry.all():
        names = (series.key, series.name, *series.aliases)
        if not needle or any(needle in normalize_alias(name) for name in names):
            label = series.key if normalize_alias(series.name) == series.key else f"{series.key} - {series.name}"
            choices.append(app_commands.Choice(name=label[:MAX_LABEL_CHARS], value=series.key))
    # Exact / prefix matches first so "AMC10" offers AMC10 before AMC10A.
    choices.sort(key=lambda c: (normalize_alias(c.value) != needle, not normalize_alias(c.value).startswith(needle)))
    return choices[:MAX_CHOICES]


async def contest_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback for a ``contest`` option that takes one series (shared by the cogs)."""
    return contest_choices(interaction.client.registry, current)  # type: ignore[attr-defined]


def problem_embed(problem: ProblemRow, *, has_image: bool) -> discord.Embed:
    """Uses only the row, so a database newer than this code (an unknown series) still works."""
    embed = discord.Embed(title=problem.title, url=problem.source_url, color=COLOR)
    if has_image:
        embed.set_image(url="attachment://problem.png")
    else:
        text = problem.statement_text
        embed.description = (text[:MAX_STATEMENT_CHARS] + " ...") if len(text) > MAX_STATEMENT_CHARS else text
        embed.add_field(
            name="Image unavailable",
            value="This problem has not been rendered yet, so the text version is shown. "
            "Diagrams and formatted maths may be missing.",
            inline=False,
        )
    footer = f"Source: {PROVIDER_NAMES.get(problem.provider, problem.provider)}"
    if problem.section:
        footer += f" | {problem.section if len(problem.section) > 1 else 'Part ' + problem.section}"
    embed.set_footer(text=footer)
    return embed


MAX_ROW_ITEMS = 5  # Discord's limit for one row of buttons


def nav_custom_id(problem_id: str, step: int, owner_id: int) -> str:
    """``mathbot:nav:+1:<owner user id>:<problem id>``."""
    return f"{NAV_PREFIX}{step:+d}:{owner_id}:{problem_id}"


def parse_nav_custom_id(custom_id: str) -> tuple[str, int, int | None] | None:
    """``(problem id, step, owner id)`` from a Previous / Next button, or None if malformed.

    Buttons posted before owners were recorded (``mathbot:nav:+1:<problem id>``) have
    no owner (None): anyone may press those.
    """
    if not custom_id.startswith(NAV_PREFIX):
        return None
    step, _, rest = custom_id[len(NAV_PREFIX) :].partition(":")
    owner, sep, problem_id = rest.partition(":")
    if not sep:  # the older form without an owner
        owner, problem_id = "", rest
    if step not in {"+1", "-1"} or not problem_id or (owner and not owner.isdigit()):
        return None
    return problem_id, int(step), int(owner) if owner else None


def problem_view(
    problem: ProblemRow,
    *,
    owner_id: int,
    has_previous: bool = False,
    has_next: bool = False,
    again: RandomFilters | None = None,
) -> discord.ui.View:
    """The buttons under a problem, in one row.

    Previous / Next (same sitting) and, only under a /random result, "Another random"
    repeating that call's filters (``again``); these work only for ``owner_id``, the
    user who ran the command. "Show answer" (private to whoever presses it) and the
    official-solution and source links work for anyone. A row holds at most five
    buttons, so the Source link is the one dropped when they don't fit: the embed
    title links to the same page.

    "Another random" is left off everywhere else: a Previous / Next message cannot
    carry the original filters (custom ids are limited to 100 characters), and a
    guessed substitute such as "this contest" silently narrowed an unfiltered /random.
    Every ``custom_id`` carries what it needs and is handled by a listener, so the
    buttons keep working after a bot restart.
    """
    items: list[discord.ui.Button] = [
        discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=nav_custom_id(problem.id, step, owner_id),
            disabled=not enabled,
        )
        for label, step, enabled in (("Previous", -1, has_previous), ("Next", +1, has_next))
    ]
    random_id = dataclasses.replace(again, owner=owner_id).custom_id() if again is not None else None
    if random_id is not None:
        items.append(discord.ui.Button(label="Another random", style=discord.ButtonStyle.primary, custom_id=random_id))
    if problem.answer:
        items.append(
            discord.ui.Button(
                label="Show answer", style=discord.ButtonStyle.secondary, custom_id=f"{ANSWER_PREFIX}{problem.id}"
            )
        )
    if problem.solution_url:
        items.append(discord.ui.Button(label="Solution", url=problem.solution_url))
    if len(items) < MAX_ROW_ITEMS:
        items.append(discord.ui.Button(label="Source", url=problem.source_url))

    view = discord.ui.View(timeout=None)
    for item in items:
        item.row = 0
        view.add_item(item)
    return view


def problem_file(data: bytes) -> discord.File:
    """The PNG as the ``problem.png`` attachment that problem_embed refers to."""
    return discord.File(io.BytesIO(data), filename="problem.png")


class Paginator(discord.ui.View):
    """Previous / Next buttons over a list of embeds, usable only by the requester."""

    def __init__(self, pages: list[discord.Embed], owner_id: int, *, timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
        self.message: discord.InteractionMessage | None = None
        for number, page in enumerate(pages, start=1):
            page.set_footer(text=f"Page {number}/{len(pages)}")
        self._sync()

    def _sync(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run the command yourself to page through it.", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):  # message deleted or no longer editable
                await self.message.edit(view=self)


async def send_paginated(interaction: discord.Interaction, pages: list[discord.Embed]) -> None:
    """Reply with ``pages``, adding Previous / Next buttons when there is more than one."""
    if len(pages) == 1:
        await interaction.response.send_message(embed=pages[0])
        return
    view = Paginator(pages, interaction.user.id)
    await interaction.response.send_message(embed=pages[0], view=view)
    view.message = await interaction.original_response()
