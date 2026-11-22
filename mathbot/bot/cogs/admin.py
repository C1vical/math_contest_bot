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
from mathbot.validate import validate

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
        settings = self.bot.settings
        report = await self.bot.db.call(
            lambda repo: validate(
                repo, settings.renders_dir, renderer_version(settings.render), registry=self.bot.registry
            )
        )
        last = await self.bot.db.call(lambda repo: dict(row) if (row := repo.last_run()) else None)

        embed = discord.Embed(
            title="Bot status",
            color=ui.COLOR if report.ok else ui.ERROR_COLOR,
            description=f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)",
        )
        for key, value in report.summary.items():
            embed.add_field(name=key.replace("_", " "), value=f"{value:,}", inline=True)
        if last:
            embed.add_field(
                name=f"Last ingestion (run {last['id']}, {last['mode']})",
                value=f"{last['status']} at {last['finished_at'] or 'unfinished'}\n"
                f"{last['inserted']} inserted, {last['updated']} updated, {last['skipped']} skipped, "
                f"{last['failed']} failed, {last['invalid']} invalid",
                inline=False,
            )
        problems = [f"[{f.severity}] {f.check}: {f.message}" for f in report.errors[:8]]
        if problems:
            embed.add_field(name="Errors", value="\n".join(problems)[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="sync", description="Re-register the bot's slash commands with Discord")
    @admin_only()
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await self.bot.sync_commands()
        await interaction.followup.send(f"Synced {count} command(s).", ephemeral=True)


async def setup(bot: MathBot) -> None:
    await bot.add_cog(Admin(bot))
