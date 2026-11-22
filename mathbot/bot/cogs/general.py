"""/help."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from mathbot.bot import ui
from mathbot.bot.policy import allowed_here

if TYPE_CHECKING:
    from mathbot.bot.app import MathBot

HELP: dict[str, tuple[str, str]] = {
    "problem": (
        "Show a specific problem",
        "`/problem contest:AMC10A year:2024 number:5`\n"
        "or everything in one box: `/problem contest:AMC 10A 2024 #5` "
        "(also `2024 AIME I P5`, `amc10a-2024-p05`).\n"
        "Contest names ignore case, spaces and punctuation and accept aliases (`aime i`, `Fall 10A`). "
        "Start typing to pick from the list.",
    ),
    "random": (
        "Show a random problem",
        "`/random` with any of: `contest` (a contest, a family such as `PCF`, `AMC`, `AIME`, or a source "
        "such as `cemc`), `year`, `year_from` / `year_to`, `number`, `difficulty`, `source`.\n"
        "`difficulty` is the problem's place in its paper (contests put easier problems first; for "
        "two-day olympiads, its place in the day). Problems shown recently in the channel are skipped.\n"
        "Example: `/random contest:AMC year_from:2015 difficulty:Hard`",
    ),
    "buttons": (
        "The buttons under a problem",
        "Previous, Next and Another random work only for the person who ran the command.\n"
        "**Show answer**: anyone can press it; only they see the answer, as a spoiler.\n"
        "**Previous / Next**: the neighbouring problems of the same contest and year.\n"
        "**Another random** (under `/random` results): the same `/random` filters again.\n"
        "**Solution / Source**: the original pages.",
    ),
    "contests": ("List all contests", "Shows every contest with the years and number of problems loaded."),
    "contest": ("Details for one contest", "`/contest contest:Euclid` - years available, format and aliases."),
    "stats": ("Database statistics", "Totals by source and the largest contests."),
    "admin": (
        "Maintenance (admins only)",
        "`/admin status` - database checks, missing images and the last ingestion run.\n"
        "`/admin sync` - register the slash commands with Discord again.",
    ),
    "help": ("This menu", "`/help` or `/help command:random`"),
}
TOPICS = {"buttons"}  # help entries that are not slash commands


def _label(name: str) -> str:
    return name.capitalize() if name in TOPICS else f"/{name}"


class General(commands.Cog):
    def __init__(self, bot: MathBot) -> None:
        self.bot = bot

    async def command_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = current.lower().lstrip("/")
        matches = [app_commands.Choice(name=_label(name), value=name) for name in HELP if needle in name]
        return matches[: ui.MAX_CHOICES]

    @app_commands.command(name="help", description="How to use the bot")
    @app_commands.describe(command="Show details for one command (or 'buttons')")
    @app_commands.autocomplete(command=command_autocomplete)
    @allowed_here()
    async def help(self, interaction: discord.Interaction, command: Optional[str] = None) -> None:
        if command is None:
            embed = discord.Embed(
                title="Math Contest Bot",
                description="Problems from AMC, AIME, USA(J)MO, IMO, CMO and the CEMC contests.\n"
                "Use `/help command:<name>` for details, e.g. `/help command:buttons`.",
                color=ui.COLOR,
            )
            embed.add_field(
                name="Commands",
                value="\n".join(f"`{_label(name)}` - {summary}" for name, (summary, _) in HELP.items()),
                inline=False,
            )
            await interaction.response.send_message(embed=embed)
            return

        name = command.lower().strip().lstrip("/")
        if name not in HELP:
            await interaction.response.send_message(
                embed=ui.error_embed("Unknown command", f"There is no `/{name}`. Try `/help`."), ephemeral=True
            )
            return
        summary, details = HELP[name]
        await interaction.response.send_message(
            embed=discord.Embed(title=f"{_label(name)} - {summary}", description=details, color=ui.COLOR)
        )


async def setup(bot: MathBot) -> None:
    await bot.add_cog(General(bot))
