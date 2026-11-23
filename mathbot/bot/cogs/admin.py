"""Admin-only maintenance commands.

Deliberately read-only apart from command sync: ingestion and rendering make
many slow network calls and belong in the CLI (``python -m mathbot refresh`` /
``render`` / ``upload``), not inside a Discord interaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from mathbot.bot import ui
from mathbot.bot.policy import admin_only
from mathbot.render.template import renderer_version
from mathbot.validate import quick_status

if TYPE_CHECKING:
    from mathbot.bot.app import MathBot


class Admin(commands.GroupCog, group_name="admin", group_description="Bot maintenance (admins only)"):
    def __init__(self, bot: MathBot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="status", description="Database health, missing renders and the last ingestion run")
    @admin_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        version = renderer_version(self.bot.settings.render)
        summary, last = await self.bot.db.call(
            lambda repo: (quick_status(repo, version), dict(row) if (row := repo.last_run()) else None)
        )
        without_image = summary["renders_missing"] + summary["renders_failed"]

        embed = discord.Embed(
            title="Bot status",
            color=ui.ERROR_COLOR if without_image else ui.COLOR,
            description=f"{summary['problems'] - without_image:,} of {summary['problems']:,} problems have an image. "
            "For a full check, run `python -m mathbot validate` where the images are rendered.",
        )
        for key, value in summary.items():
            embed.add_field(name=key.replace("_", " "), value=f"{value:,}", inline=True)
        if last:
            embed.add_field(
                name=f"Last ingestion (run {last['id']}, {last['mode']})",
                value=f"{last['status']} at {last['finished_at'] or 'unfinished'}\n"
                f"{last['inserted']} inserted, {last['updated']} updated, {last['skipped']} skipped, "
                f"{last['failed']} failed, {last['invalid']} invalid",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="sync", description="Re-register the bot's slash commands with Discord")
    @admin_only()
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await self.bot.sync_commands()
        await interaction.followup.send(f"Synced {count} command(s).", ephemeral=True)


async def setup(bot: MathBot) -> None:
    await bot.add_cog(Admin(bot))
