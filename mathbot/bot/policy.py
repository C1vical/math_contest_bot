"""Who may use the bot, and where. Pure functions plus thin ``app_commands`` checks."""

from __future__ import annotations

from collections.abc import Iterable

import discord
from discord import app_commands

from mathbot.config import BotSettings


class NotAllowedHere(app_commands.CheckFailure):
    """Command used outside the configured channels / without an allowed role."""


class NotAdmin(app_commands.CheckFailure):
    """Maintenance command used by a non-admin."""


def is_allowed(settings: BotSettings, channel_ids: Iterable[int], role_ids: Iterable[int]) -> bool:
    """Channel and role allow-lists; an empty list means "no restriction".

    ``channel_ids`` holds the channel and, for threads, its parent channel.
    """
    channel_ok = not settings.allowed_channel_ids or bool(settings.allowed_channel_ids & set(channel_ids))
    role_ok = not settings.allowed_role_ids or bool(settings.allowed_role_ids & set(role_ids))
    return channel_ok and role_ok


def is_admin(settings: BotSettings, user_id: int, role_ids: Iterable[int], *, manage_guild: bool) -> bool:
    """Admins are listed users, holders of an admin role, or - when neither list is
    configured - members with the Manage Server permission."""
    if user_id in settings.admin_user_ids or settings.admin_role_ids & set(role_ids):
        return True
    if settings.admin_user_ids or settings.admin_role_ids:
        return False
    return manage_guild


def _context(interaction: discord.Interaction) -> tuple[list[int], list[int], bool]:
    channel_ids = [interaction.channel_id] if interaction.channel_id else []
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id:
        channel_ids.append(parent_id)
    user = interaction.user
    role_ids = [role.id for role in getattr(user, "roles", [])]
    permissions = getattr(user, "guild_permissions", None)
    return channel_ids, role_ids, bool(permissions and permissions.manage_guild)


def allowed_here():
    async def predicate(interaction: discord.Interaction) -> bool:
        settings: BotSettings = interaction.client.settings.bot  # type: ignore[attr-defined]
        channel_ids, role_ids, _ = _context(interaction)
        if not is_allowed(settings, channel_ids, role_ids):
            raise NotAllowedHere()
        return True

    return app_commands.check(predicate)


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        settings: BotSettings = interaction.client.settings.bot  # type: ignore[attr-defined]
        _, role_ids, manage_guild = _context(interaction)
        if not is_admin(settings, interaction.user.id, role_ids, manage_guild=manage_guild):
            raise NotAdmin()
        return True

    return app_commands.check(predicate)


def cooldown_for(
    settings: BotSettings, user_id: int, role_ids: Iterable[int] = (), *, manage_guild: bool = False
) -> app_commands.Cooldown | None:
    """One problem command per ``COMMAND_COOLDOWN_SECONDS`` per user; admins (as in is_admin) are exempt."""
    if settings.command_cooldown <= 0 or is_admin(settings, user_id, role_ids, manage_guild=manage_guild):
        return None
    return app_commands.Cooldown(1, settings.command_cooldown)


def per_user_cooldown():
    def factory(interaction: discord.Interaction) -> app_commands.Cooldown | None:
        settings: BotSettings = interaction.client.settings.bot  # type: ignore[attr-defined]
        _, role_ids, manage_guild = _context(interaction)
        return cooldown_for(settings, interaction.user.id, role_ids, manage_guild=manage_guild)

    return app_commands.checks.dynamic_cooldown(factory, key=lambda interaction: interaction.user.id)
