"""/problem and /random, plus the "Show answer" button."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from mathbot.bot import ui
from mathbot.bot.policy import allowed_here, per_user_cooldown
from mathbot.contests import PROVIDER_NAMES, PROVIDERS, ContestSeries, UnknownContestError, compress_years
from mathbot.db.repo import ProblemRow

if TYPE_CHECKING:
    from mathbot.bot.app import MathBot

SOURCE_CHOICES = [app_commands.Choice(name=PROVIDER_NAMES[p], value=p) for p in PROVIDERS]


class Problems(commands.Cog):
    def __init__(self, bot: MathBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ helpers

    async def _unknown_contest(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.send_message(embed=ui.unknown_contest_embed(self.bot.registry, text), ephemeral=True)

    async def _send(self, interaction: discord.Interaction, problem: ProblemRow) -> None:
        image = await self.bot.images.get(problem)
        embed = ui.problem_embed(problem, has_image=image is not None)
        view = ui.problem_view(problem)
        if image is not None:
            await interaction.followup.send(embed=embed, file=ui.problem_file(image), view=view)
        else:
            await interaction.followup.send(embed=embed, view=view)

    async def _explain_missing(self, series: ContestSeries, year: int, number: int) -> discord.Embed:
        """Say precisely which part of contest / year / number does not exist."""
        years = await self.bot.db.call(lambda repo: repo.years_for(series.key))
        if not years:
            return ui.error_embed(
                "No problems loaded", f"**{series.name}** is supported, but none of its problems are loaded yet."
            )
        if year not in years:
            return ui.error_embed(
                "Year not available",
                f"There is no {year} **{series.name}** in the database.\nAvailable years: {compress_years(years)}",
            )
        numbers = await self.bot.db.call(lambda repo: repo.numbers_for(series.key, year))
        return ui.error_embed(
            "Problem not available",
            f"{year} **{series.name}** has no problem {number}.\nAvailable problems: {compress_years(numbers)}",
        )

    # ------------------------------------------------------------------ autocomplete

    async def contest_or_family_autocomplete(self, interaction: discord.Interaction, current: str):
        return ui.contest_choices(self.bot.registry, current, families=True)

    async def year_autocomplete(self, interaction: discord.Interaction, current: str):
        series = self.bot.registry.resolve(str(getattr(interaction.namespace, "contest", "") or ""))
        if series is None:
            return []
        years = await self.bot.db.call(lambda repo: repo.years_for(series.key))
        matching = [y for y in reversed(years) if str(y).startswith(current.strip())]
        return [app_commands.Choice(name=str(y), value=y) for y in matching[: ui.MAX_CHOICES]]

    # ------------------------------------------------------------------ commands

    @app_commands.command(name="problem", description="Show a specific contest problem")
    @app_commands.describe(
        contest="Contest name, e.g. AMC10A, AIME I, Euclid",
        year="Contest year, e.g. 2024",
        number="Problem number",
    )
    @app_commands.autocomplete(contest=ui.contest_autocomplete, year=year_autocomplete)
    @allowed_here()
    @per_user_cooldown()
    async def problem(
        self,
        interaction: discord.Interaction,
        contest: str,
        year: app_commands.Range[int, 1900, 2100],
        number: app_commands.Range[int, 1, 60],
    ) -> None:
        series = self.bot.registry.resolve(contest)
        if series is None:
            await self._unknown_contest(interaction, contest)
            return
        # The lookup is a local, indexed query, so it runs before deferring: that keeps
        # "not found" replies ephemeral (a follow-up to a public defer cannot be).
        problem = await self.bot.db.call(lambda repo: repo.get_problem(series.key, year, number))
        if problem is None:
            embed = await self._explain_missing(series, year, number)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)  # image delivery may be slow
        await self._send(interaction, problem)

    @app_commands.command(name="random", description="Show a random problem, optionally filtered")
    @app_commands.describe(
        contest="Contest or family, e.g. AMC10A, AIME, PCF",
        year="Only problems from this year",
        number="Only problems with this number",
        source="Only problems from this source",
    )
    @app_commands.autocomplete(contest=contest_or_family_autocomplete)
    @app_commands.choices(source=SOURCE_CHOICES)
    @allowed_here()
    @per_user_cooldown()
    async def random(
        self,
        interaction: discord.Interaction,
        contest: Optional[str] = None,
        year: Optional[app_commands.Range[int, 1900, 2100]] = None,
        number: Optional[app_commands.Range[int, 1, 60]] = None,
        source: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        keys: list[str] | None = None
        if contest is not None:
            try:
                keys = [s.key for s in self.bot.registry.select(contest)]
            except UnknownContestError:
                await self._unknown_contest(interaction, contest)
                return
        provider = source.value if source else None

        problem = await self.bot.db.call(
            lambda repo: repo.random_problem(series_keys=keys, year=year, number=number, provider=provider)
        )
        if problem is None:
            filters = [
                f"contest `{contest}`" if contest else None,
                f"year {year}" if year else None,
                f"problem number {number}" if number else None,
                f"source {source.name}" if source else None,
            ]
            used = ", ".join(f for f in filters if f)
            description = f"No problem matches {used}." if used else "The database has no problems yet."
            await interaction.response.send_message(embed=ui.error_embed("Nothing found", description), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)  # image delivery may be slow
        await self._send(interaction, problem)

    # ------------------------------------------------------------------ answer button

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        if not custom_id.startswith(ui.ANSWER_PREFIX) or interaction.response.is_done():
            return
        problem = await self.bot.db.call(lambda repo: repo.get_problem_by_id(custom_id[len(ui.ANSWER_PREFIX) :]))
        if problem is None or not problem.answer:
            await interaction.response.send_message("No answer is stored for this problem.", ephemeral=True)
            return
        await interaction.response.send_message(f"**{problem.title}** - answer: ||{problem.answer}||", ephemeral=True)


async def setup(bot: MathBot) -> None:
    await bot.add_cog(Problems(bot))
