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
        "Contest names are case-insensitive and accept aliases (`aime i`, `AMC 10A`). "
        "Start typing to pick from the list.",
    ),
    "gimme": ("Same as /problem", "`/gimme year:2024 contest:AMC10A question_number:5` - kept for old habits."),
    "random": (
        "Show a random problem",
        "`/random` with any of `contest`, `year`, `number`, `source`.\n"
        "`contest` also accepts a family such as `PCF`, `FGH`, `AMC` or `AIME`.",
    ),
    "contests": ("List all contests", "Shows every contest with the years and number of problems loaded."),
    "contest": ("Details for one contest", "`/contest contest:Euclid` - years available, format and aliases."),
    "info": ("Same as /contest", "`/info contest:Euclid`"),
    "stats": ("Database statistics", "Totals by source and the largest contests."),
    "help": ("This menu", "`/help` or `/help command:random`"),
}


class General(commands.Cog):
    def __init__(self, bot: MathBot) -> None:
        self.bot = bot

    async def command_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = current.lower().lstrip("/")
        return [app_commands.Choice(name=f"/{name}", value=name) for name in HELP if needle in name][: ui.MAX_CHOICES]

    @app_commands.command(name="help", description="How to use the bot")
    @app_commands.describe(command="Show details for one command")
    @app_commands.autocomplete(command=command_autocomplete)
    @allowed_here()
    async def help(self, interaction: discord.Interaction, command: Optional[str] = None) -> None:
        if command is None:
            embed = discord.Embed(
                title="Math Contest Bot",
                description="Problems from AMC, AIME, USA(J)MO, IMO, CMO and the CEMC contests.\n"
                "Use `/help command:<name>` for details.",
                color=ui.COLOR,
            )
            embed.add_field(
                name="Commands",
                value="\n".join(f"`/{name}` - {summary}" for name, (summary, _) in HELP.items()),
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
            embed=discord.Embed(title=f"/{name} - {summary}", description=details, color=ui.COLOR)
        )


async def setup(bot: MathBot) -> None:
    await bot.add_cog(General(bot))
