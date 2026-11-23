"""/contests, /contest and /stats."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from mathbot.bot import ui
from mathbot.bot.policy import allowed_here
from mathbot.contests import FAMILY_NAMES, PROVIDER_NAMES, compress_years
from mathbot.db.repo import SeriesStats

if TYPE_CHECKING:
    from mathbot.bot.app import MathBot

FAMILIES_PER_PAGE = 4


def _line(stats: SeriesStats) -> str:
    if not stats.problems:
        return f"`{stats.key}` - {stats.name} *(not loaded)*"
    years = str(stats.first_year) if stats.first_year == stats.last_year else f"{stats.first_year}-{stats.last_year}"
    return f"`{stats.key}` - {stats.name} ({years}, {stats.problems} problems)"


def contest_pages(stats: list[SeriesStats]) -> list[discord.Embed]:
    """One embed per few families so long lists never exceed Discord's limits."""
    families: dict[str, list[SeriesStats]] = {}
    for s in stats:
        families.setdefault(s.family, []).append(s)
    names = list(families)
    pages = []
    for start in range(0, len(names), FAMILIES_PER_PAGE):
        embed = discord.Embed(
            title="Available contests",
            description="Use the name in backticks with `/problem`, `/random` or `/contest`.",
            color=ui.COLOR,
        )
        for family in names[start : start + FAMILIES_PER_PAGE]:
            value = "\n".join(_line(s) for s in families[family])
            embed.add_field(name=FAMILY_NAMES.get(family, family), value=value[:1024], inline=False)
        pages.append(embed)
    return pages or [discord.Embed(title="Available contests", description="No contests are enabled.", color=ui.COLOR)]


class Contests(commands.Cog):
    def __init__(self, bot: MathBot) -> None:
        self.bot = bot

    @app_commands.command(name="contests", description="List the contests the bot knows")
    @allowed_here()
    async def contests(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)  # counts every problem; slow on a small host
        stats = await self.bot.db.call(lambda repo: repo.series_stats())
        await ui.send_paginated(interaction, contest_pages(stats))

    @app_commands.command(name="contest", description="Details and available years for one contest")
    @app_commands.describe(contest="Contest name, e.g. AMC10A, AIME I, Euclid")
    @app_commands.autocomplete(contest=ui.contest_autocomplete)
    @allowed_here()
    async def contest(self, interaction: discord.Interaction, contest: str) -> None:
        series = self.bot.registry.resolve(contest)
        if series is None:
            embed = ui.unknown_contest_embed(self.bot.registry, contest)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        years, stats = await self.bot.db.call(
            lambda repo: (repo.years_for(series.key), [s for s in repo.series_stats() if s.key == series.key])
        )
        loaded = stats[0].problems if stats else 0

        embed = discord.Embed(title=f"{series.name} ({series.key})", color=ui.COLOR)
        if series.notes:
            embed.description = series.notes
        embed.add_field(name="Source", value=PROVIDER_NAMES[series.provider], inline=True)
        embed.add_field(name="Family", value=FAMILY_NAMES.get(series.family, series.family), inline=True)
        embed.add_field(name="Problems loaded", value=str(loaded), inline=True)
        embed.add_field(name="Years available", value=compress_years(years)[:1024], inline=False)
        if series.eras:
            eras = "\n".join(
                f"{era.first_year}-{era.last_year or 'present'}: {era.problems} problems" for era in series.eras
            )
            embed.add_field(name="Format", value=eras, inline=False)
        if series.aliases:
            embed.add_field(name="Also accepted", value=", ".join(f"`{a}`" for a in series.aliases), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="stats", description="How many problems are loaded")
    @allowed_here()
    async def stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        totals, stats = await self.bot.db.call(lambda repo: (repo.totals(), repo.series_stats(include_empty=False)))
        embed = discord.Embed(title="Database statistics", color=ui.COLOR)
        embed.add_field(name="Problems", value=f"{totals['problems']:,}", inline=True)
        embed.add_field(name="Contest sittings", value=f"{totals['editions']:,}", inline=True)
        embed.add_field(name="With answers", value=f"{totals['with_answers']:,}", inline=True)
        embed.add_field(name="Rendered images", value=f"{totals['renders_ok']:,}", inline=True)
        by_source: dict[str, int] = {}
        for s in stats:
            by_source[s.provider] = by_source.get(s.provider, 0) + s.problems
        if by_source:
            lines = [f"{PROVIDER_NAMES[p]}: {n:,}" for p, n in sorted(by_source.items(), key=lambda kv: -kv[1])]
            embed.add_field(name="By source", value="\n".join(lines), inline=False)
        top = sorted(stats, key=lambda s: -s.problems)[:10]
        if top:
            embed.add_field(
                name="Largest contests", value="\n".join(f"`{s.key}`: {s.problems:,}" for s in top), inline=False
            )
        await interaction.followup.send(embed=embed)


async def setup(bot: MathBot) -> None:
    await bot.add_cog(Contests(bot))
