"""Bot startup, shutdown and the global interaction error handler."""

from __future__ import annotations

import logging
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from mathbot.bot import ui
from mathbot.bot.health import HealthServer, HealthState
from mathbot.bot.images import ImageService
from mathbot.bot.policy import NotAdmin, NotAllowedHere
from mathbot.bot.services import Database
from mathbot.config import ConfigError, Settings, require_bot_token
from mathbot.contests import ContestRegistry
from mathbot.db import Repository, open_database

log = logging.getLogger(__name__)

MAX_ECHO_CHARS = 80  # user input repeated back in an error message

COGS = (
    "mathbot.bot.cogs.problems",
    "mathbot.bot.cogs.contests",
    "mathbot.bot.cogs.general",
    "mathbot.bot.cogs.admin",
)


class MathBot(commands.Bot):
    """The Discord client: settings, registry, database access and image delivery for the cogs."""

    def __init__(self, settings: Settings, registry: ContestRegistry, health: HealthState) -> None:
        # Slash commands only: no privileged intents, and the prefix is never matched.
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(), help_command=None)
        self.settings = settings
        self.registry = registry
        self.health = health
        self.db = Database(settings.database_path)
        self.images = ImageService(settings, self.db)
        self._announced = False
        self.tree.on_error = self.on_app_command_error  # type: ignore[method-assign]

    async def setup_hook(self) -> None:
        for extension in COGS:
            await self.load_extension(extension)
        if self.settings.bot.sync_commands:
            await self.sync_commands()

    async def sync_commands(self) -> int:
        """Register slash commands, to one guild when ``GUILD_ID`` is set (instant) or globally."""
        guild_id = self.settings.bot.guild_id
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("slash commands synced", extra={"count": len(synced), "guild": guild_id or "global"})
        return len(synced)

    async def on_ready(self) -> None:
        # on_ready fires again after every reconnect; announce only once per process.
        self.health.ready = True
        log.info("connected", extra={"user": str(self.user), "guilds": len(self.guilds)})
        channel_id = self.settings.bot.announce_channel_id
        if self._announced or not channel_id:
            return
        self._announced = True
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("ANNOUNCE_CHANNEL_ID is not a channel I can see", extra={"channel": channel_id})
            return
        try:
            await channel.send("**Math Contest Bot is online!** Type `/` to see the available commands.")
        except discord.HTTPException as exc:
            log.warning("could not send the online announcement", extra={"error": str(exc)})

    async def on_disconnect(self) -> None:
        self.health.ready = False

    async def on_resumed(self) -> None:
        self.health.ready = True

    async def close(self) -> None:
        self.health.ready = False
        await self.images.close()
        await super().close()

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Every failure gets a reply the user can see, and unexpected ones a log entry."""
        if isinstance(error, NotAllowedHere):
            message = "This bot's commands can't be used here (wrong channel, or you lack the required role)."
        elif isinstance(error, NotAdmin):
            message = "That command is for bot administrators only."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down - try again in {ui.seconds(error.retry_after)}."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You can't use that command here."
        elif isinstance(error, app_commands.TransformerError):
            value = discord.utils.escape_markdown(str(error.value))[:MAX_ECHO_CHARS]
            message = f"I couldn't understand `{value}` for that option."
        else:
            reference = uuid.uuid4().hex[:8]
            original = getattr(error, "original", error)
            log.error(
                "unhandled interaction error",
                exc_info=original,
                extra={
                    "reference": reference,
                    "command": interaction.command.qualified_name if interaction.command else None,
                    "user": interaction.user.id,
                },
            )
            message = f"Something went wrong on my side. If it keeps happening, mention reference `{reference}`."
        await respond_ephemeral(interaction, message)


async def respond_ephemeral(interaction: discord.Interaction, message: str) -> None:
    """Reply privately whether or not the interaction was already acknowledged."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as exc:
        # The interaction token expired or Discord is unreachable: nothing more can be shown.
        log.warning("could not deliver error message", extra={"error": str(exc)})


def install_snapshot(settings: Settings) -> None:
    """Fetch the newest published database; fall back to the local copy if that fails."""
    from mathbot.snapshot import SnapshotError, fetch_snapshot

    try:
        manifest = fetch_snapshot(settings.database_snapshot_url or "", settings.database_path)
    except SnapshotError as exc:
        if not settings.database_path.is_file():
            raise ConfigError(f"no local database and the snapshot could not be installed: {exc}") from exc
        log.warning("snapshot unavailable; using the existing local database", extra={"error": str(exc)})
        return
    if manifest is None:
        log.info("local database already matches the published snapshot")
    else:
        log.info("installed published database", extra={"problems": manifest.get("problems")})


def run_bot(settings: Settings) -> int:
    """Validate configuration, prepare the database, then run until shutdown."""
    token = require_bot_token(settings)
    registry = ContestRegistry(enable=settings.enable_contests, disable=settings.disable_contests)

    if settings.database_snapshot_url:
        install_snapshot(settings)

    conn = open_database(settings.database_path)  # creates data/ and applies migrations
    try:
        repo = Repository(conn)
        repo.sync_registry(registry)
        problems = repo.totals()["problems"]
    finally:
        conn.close()
    if problems == 0:
        log.warning("the database has no problems yet; run 'python -m mathbot refresh' then 'render'")

    health = HealthState()
    bot = MathBot(settings, registry, health)
    health.db_check = bot.db.ping

    server: HealthServer | None = None
    if settings.bot.health_enabled:
        server = HealthServer(health, settings.bot.health_host, settings.bot.health_port)
        try:
            server.start()
        except OSError as exc:
            raise ConfigError(f"cannot bind health endpoint to port {settings.bot.health_port}: {exc}") from exc

    try:
        bot.run(token, log_handler=None)  # handles SIGINT/SIGTERM and closes the bot cleanly
    except discord.LoginFailure:
        log.error("Discord rejected DISCORD_TOKEN; check the value in .env")
        return 1
    finally:
        if server is not None:
            server.stop()
    return 0
