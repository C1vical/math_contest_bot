"""/problem and /random, and the buttons under every problem.

Buttons (Show answer, Previous, Next, Another random) carry everything they need in
their ``custom_id`` and are handled by :meth:`Problems.on_interaction`, so messages
posted before a restart keep working. Button presses obey the same channel / role
rules as the commands and have their own per-user cooldown.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from mathbot.bot import ui
from mathbot.bot.policy import allowed_here, cooldown_for, interaction_context, is_allowed, per_user_cooldown
from mathbot.bot.query import MAX_NUMBER, MAX_YEAR, MIN_YEAR, RandomFilters, parse_problem_reference
from mathbot.contests import PROVIDER_NAMES, PROVIDERS, ContestSeries, UnknownContestError, compress_years
from mathbot.db.repo import ProblemRow

if TYPE_CHECKING:
    from mathbot.bot.app import MathBot

SOURCE_CHOICES = [app_commands.Choice(name=PROVIDER_NAMES[p], value=p) for p in PROVIDERS]
DIFFICULTY_CHOICES = [
    app_commands.Choice(name="Easy (first third of the paper)", value="easy"),
    app_commands.Choice(name="Medium (middle third)", value="medium"),
    app_commands.Choice(name="Hard (last third)", value="hard"),
]
RECENT_PER_CHANNEL = 50  # /random avoids repeating any of the last N problems shown in a channel
MAX_COOLDOWN_BUCKETS = 2000

Year = app_commands.Range[int, MIN_YEAR, MAX_YEAR]
Number = app_commands.Range[int, 1, MAX_NUMBER]

PROBLEM_FORMAT_HELP = (
    "Give the contest, year and number, e.g. `/problem contest:AMC10A year:2019 number:25`, "
    "or all in one: `/problem contest:AMC 10A 2019 #25`."
)


class Problems(commands.Cog):
    def __init__(self, bot: MathBot) -> None:
        self.bot = bot
        self._recent: dict[int, deque[str]] = {}
        self._button_cooldowns: dict[int, app_commands.Cooldown] = {}

    # ------------------------------------------------------------------ helpers

    async def _unknown_contest(self, interaction: discord.Interaction, text: str) -> None:
        await self._reply_error(interaction, ui.unknown_contest_embed(self.bot.registry, text))

    @staticmethod
    async def _reply_error(interaction: discord.Interaction, embed: discord.Embed) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    def _remember(self, interaction: discord.Interaction, problem: ProblemRow) -> None:
        recent = self._recent.setdefault(interaction.channel_id or 0, deque(maxlen=RECENT_PER_CHANNEL))
        recent.append(problem.id)

    async def _send(
        self, interaction: discord.Interaction, problem: ProblemRow, again: RandomFilters | None = None
    ) -> None:
        """Post ``problem`` publicly. The interaction must already be deferred."""
        has_previous, has_next = await self.bot.db.call(
            lambda repo: (repo.neighbour(problem.id, -1) is not None, repo.neighbour(problem.id, +1) is not None)
        )
        image = await self.bot.images.get(problem)
        embed = ui.problem_embed(problem, has_image=image is not None)
        view = ui.problem_view(problem, has_previous=has_previous, has_next=has_next, again=again)
        self._remember(interaction, problem)
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

    async def _pick_random(self, interaction: discord.Interaction, filters: RandomFilters) -> ProblemRow | None:
        """A problem matching ``filters``, avoiding the ones shown recently in this channel.

        Raises :class:`UnknownContestError` when ``filters.contest`` names nothing.
        """
        keys = [s.key for s in self.bot.registry.select(filters.contest)] if filters.contest else None
        recent = tuple(self._recent.get(interaction.channel_id or 0, ()))

        def query(repo, exclude):
            return repo.random_problem(
                series_keys=keys,
                year=filters.year,
                year_from=filters.year_from,
                year_to=filters.year_to,
                number=filters.number,
                provider=filters.source,
                difficulty=filters.difficulty,
                exclude_ids=exclude,
            )

        # Everything matching was shown recently: a repeat beats "nothing found".
        return await self.bot.db.call(lambda repo: query(repo, recent) or (query(repo, ()) if recent else None))

    def _button_retry_after(self, interaction: discord.Interaction) -> float | None:
        """Seconds the user must wait before the next button press, or None (go ahead)."""
        _, role_ids, manage_guild = interaction_context(interaction)
        template = cooldown_for(self.bot.settings.bot, interaction.user.id, role_ids, manage_guild=manage_guild)
        if template is None:
            return None
        if len(self._button_cooldowns) > MAX_COOLDOWN_BUCKETS:  # forget users whose cooldown has passed
            self._button_cooldowns = {
                user: bucket for user, bucket in self._button_cooldowns.items() if bucket.get_tokens() < bucket.rate
            }
        bucket = self._button_cooldowns.setdefault(interaction.user.id, template)
        return bucket.update_rate_limit()

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
        contest="Contest name (AMC10A, AIME I, Euclid), or everything at once: AMC 10A 2019 #25",
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
        year: Optional[Year] = None,
        number: Optional[Number] = None,
    ) -> None:
        series = self.bot.registry.resolve(contest)
        name = contest
        if series is None or year is None or number is None:
            ref = parse_problem_reference(contest, lambda text: self.bot.registry.resolve(text) is not None)
            name = ref.contest or contest
            series = self.bot.registry.resolve(name)
            year = year if year is not None else ref.year
            number = number if number is not None else ref.number
        if series is None:
            await self._unknown_contest(interaction, name)
            return
        if year is None or number is None:
            years = await self.bot.db.call(lambda repo: repo.years_for(series.key))
            missing = (
                "year and problem number"
                if year is None and number is None
                else ("year" if year is None else "problem number")
            )
            await self._reply_error(
                interaction,
                ui.error_embed(
                    f"Which {series.name} problem?",
                    f"The {missing} is missing. {PROBLEM_FORMAT_HELP}\n"
                    f"Years available for **{series.name}**: {compress_years(years)}",
                ),
            )
            return
        # The lookup is a local, indexed query, so it runs before deferring: that keeps
        # "not found" replies ephemeral (a follow-up to a public defer cannot be).
        problem = await self.bot.db.call(lambda repo: repo.get_problem(series.key, year, number))
        if problem is None:
            await self._reply_error(interaction, await self._explain_missing(series, year, number))
            return
        await interaction.response.defer(thinking=True)  # image delivery may be slow
        await self._send(interaction, problem)

    @app_commands.command(name="random", description="Show a random problem, optionally filtered")
    @app_commands.describe(
        contest="Contest, family or source, e.g. AMC10A, AIME, PCF, cemc",
        year="Only problems from this year",
        year_from="Only problems from this year onward",
        year_to="Only problems up to this year",
        number="Only problems with this number",
        difficulty="Position in the paper: contests put easier problems first",
        source="Only problems from this source",
    )
    @app_commands.autocomplete(contest=contest_or_family_autocomplete)
    @app_commands.choices(source=SOURCE_CHOICES, difficulty=DIFFICULTY_CHOICES)
    @allowed_here()
    @per_user_cooldown()
    async def random(
        self,
        interaction: discord.Interaction,
        contest: Optional[str] = None,
        year: Optional[Year] = None,
        year_from: Optional[Year] = None,
        year_to: Optional[Year] = None,
        number: Optional[Number] = None,
        difficulty: Optional[app_commands.Choice[str]] = None,
        source: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        if year_from is not None and year_to is not None and year_from > year_to:
            year_from, year_to = year_to, year_from
        filters = RandomFilters(
            contest=contest.strip() if contest else None,
            year=year,
            year_from=year_from,
            year_to=year_to,
            number=number,
            difficulty=difficulty.value if difficulty else None,
            source=source.value if source else None,
        )
        await self._random(interaction, filters)

    async def _random(self, interaction: discord.Interaction, filters: RandomFilters) -> None:
        try:
            problem = await self._pick_random(interaction, filters)
        except UnknownContestError:
            await self._unknown_contest(interaction, filters.contest or "")
            return
        if problem is None:
            used = filters.describe()
            description = f"No problem matches {used}." if used else "The database has no problems yet."
            await self._reply_error(interaction, ui.error_embed("Nothing found", description))
            return
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)  # image delivery may be slow
        await self._send(interaction, problem, again=self._button_filters(filters))

    @staticmethod
    def _button_filters(filters: RandomFilters) -> RandomFilters:
        """``filters`` with the contest in its compact form, so it fits in a button id."""
        if not filters.contest:
            return filters
        compact = "".join(ch for ch in filters.contest.upper() if ch.isalnum())
        return dataclasses.replace(filters, contest=compact)

    # ------------------------------------------------------------------ buttons

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component or interaction.response.is_done():
            return
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        if custom_id.startswith(ui.ANSWER_PREFIX):
            await self._show_answer(interaction, custom_id[len(ui.ANSWER_PREFIX) :])
            return
        nav = ui.parse_nav_custom_id(custom_id)
        filters = RandomFilters.from_custom_id(custom_id)
        if nav is None and filters is None:
            return  # not ours (e.g. the contest list's pagination buttons)

        channel_ids, role_ids, _ = interaction_context(interaction)
        if not is_allowed(self.bot.settings.bot, channel_ids, role_ids):
            await interaction.response.send_message(
                "This bot's commands can't be used here (wrong channel, or you lack the required role).",
                ephemeral=True,
            )
            return
        retry_after = self._button_retry_after(interaction)
        if retry_after is not None:
            await interaction.response.send_message(
                f"Slow down - try again in {ui.seconds(retry_after)}.", ephemeral=True
            )
            return

        if filters is not None:
            await self._random(interaction, filters)
            return
        problem_id, step = nav  # type: ignore[misc]
        problem = await self.bot.db.call(lambda repo: repo.neighbour(problem_id, step))
        if problem is None:
            where = "next" if step > 0 else "previous"
            await interaction.response.send_message(f"There is no {where} problem in this contest.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await self._send(interaction, problem)

    async def _show_answer(self, interaction: discord.Interaction, problem_id: str) -> None:
        problem = await self.bot.db.call(lambda repo: repo.get_problem_by_id(problem_id))
        if problem is None or not problem.answer:
            await interaction.response.send_message("No answer is stored for this problem.", ephemeral=True)
            return
        await interaction.response.send_message(f"**{problem.title}** - answer: ||{problem.answer}||", ephemeral=True)


async def setup(bot: MathBot) -> None:
    await bot.add_cog(Problems(bot))
