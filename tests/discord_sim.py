"""A stand-in for Discord: drives the real bot with raw interaction payloads, no network.

The bot's command tree, checks, cooldowns, autocomplete and button listeners all run as they
do in production. Only discord.py's webhook adapter is replaced: each reply is checked against
Discord's documented limits, and one that breaks them fails with HTTP 400 as it would for real.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import types
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import discord
from discord.webhook.async_ import AsyncWebhookAdapter

from mathbot.bot.app import MathBot
from mathbot.bot.health import HealthState

APPLICATION, GUILD, CHANNEL, ROLE = 42, 7, 10, 5
COMMAND, COMPONENT, AUTOCOMPLETE = 2, 3, 4  # interaction types
BUTTON, LINK_STYLE = 2, 5
_TASKS = ("discord.py: on_", "CommandTree-invoker", "discord-ui-view-dispatch")
_OPTION_TYPES = {str: 3, int: 4, bool: 5}


def message_problems(data: dict, *, has_files: bool = False, edit: bool = False) -> list[str]:
    """Why Discord would refuse this message (empty when it would accept it)."""
    out = []
    if len(data.get("content") or "") > 2000:
        out.append("content over 2000 characters")
    embeds = data.get("embeds") or []
    if not (data.get("content") or embeds or has_files or edit):
        out.append("empty message")
    total = 0
    for embed in embeds:
        title, description = embed.get("title") or "", embed.get("description") or ""
        footer, fields = (embed.get("footer") or {}).get("text") or "", embed.get("fields") or []
        out += [f"embed {n} is {len(v)}" for n, v, m in (("title", title, 256), ("footer", footer, 2048)) if len(v) > m]
        if len(description) > 4096:
            out.append(f"embed description is {len(description)}")
        if len(fields) > 25:
            out.append(f"embed has {len(fields)} fields")
        for item in fields:
            if not 1 <= len(item["name"].strip()) <= 256 or not 1 <= len(item["value"].strip()) <= 1024:
                out.append(f"embed field {item['name'][:30]!r} has a value of {len(item['value'])}")
        if embed.get("url") and urlsplit(embed["url"]).scheme not in {"http", "https"}:
            out.append(f"embed url {embed['url']!r}")
        total += len(title) + len(description) + len(footer) + sum(len(f["name"]) + len(f["value"]) for f in fields)
    if total > 6000:
        out.append(f"embeds total {total} characters")
    ids: list[str] = []
    rows = data.get("components") or []
    if len(rows) > 5:
        out.append(f"{len(rows)} rows")
    for row in rows:
        if not 1 <= len(row["components"]) <= 5:
            out.append(f"{len(row['components'])} buttons in a row")
        for item in row["components"]:
            if not 1 <= len(item.get("label") or "") <= 80:
                out.append(f"button label {item.get('label')!r}")
            if item["style"] == LINK_STYLE:
                if urlsplit(item["url"]).scheme not in {"http", "https"} or len(item["url"]) > 512:
                    out.append(f"button url {item['url']!r}")
            else:
                ids.append(item["custom_id"])
                if not 1 <= len(item["custom_id"]) <= 100:
                    out.append(f"custom_id of {len(item['custom_id'])}")
    if len(ids) != len(set(ids)):
        out.append("duplicate custom_id")
    return out


def choice_problems(choices: list[dict]) -> list[str]:
    out = [f"{len(choices)} choices"] if len(choices) > 25 else []
    out += [f"choice name {c['name']!r}" for c in choices if not 1 <= len(c["name"]) <= 100]
    out += [f"choice value {c['value']!r}" for c in choices if not 1 <= len(str(c["value"])) <= 100]
    return out


@dataclass
class Reply:
    """Everything the bot sent for one interaction."""

    messages: list[dict] = field(default_factory=list)  # first replies and follow-ups, in order
    edits: list[dict] = field(default_factory=list)
    choices: list[dict] | None = None
    deferred: bool = False
    rejected: list[str] = field(default_factory=list)  # limits broken: Discord would have refused
    errors: list[str] = field(default_factory=list)  # logged at ERROR while handling it
    first_reply_seconds: float | None = None
    message_id: str | None = None  # of the last message sent

    @property
    def message(self) -> dict:
        assert len(self.messages) == 1, (self.messages, self.rejected, self.errors)
        return self.messages[0]

    @property
    def ephemeral(self) -> bool:
        return bool(self.message.get("flags", 0) & 64)

    @property
    def text(self) -> str:
        """All the visible words of the reply."""
        parts = [self.message.get("content") or ""]
        for embed in self.message.get("embeds") or []:
            parts += [embed.get("title") or "", embed.get("description") or ""]
            parts += [f"{f['name']} {f['value']}" for f in embed.get("fields") or []]
        return "\n".join(filter(None, parts))

    @property
    def title(self) -> str:
        return (self.message.get("embeds") or [{}])[0].get("title") or ""

    def button(self, label: str) -> dict | None:
        rows = self.message.get("components") or []
        return next((b for row in rows for b in row["components"] if b["label"] == label), None)

    @property
    def buttons(self) -> list[str]:
        return [b["label"] for row in self.message.get("components") or [] for b in row["components"]]

    @property
    def ok(self) -> bool:
        """Discord got exactly one valid answer and nothing went wrong behind it."""
        answered = len(self.messages) == 1 or len(self.edits) == 1 or self.choices is not None
        return answered and not self.rejected and not self.errors


class _ErrorLog(logging.Handler):
    def __init__(self, sim: DiscordSim) -> None:
        super().__init__(logging.ERROR)
        self.sim = sim

    def emit(self, record: logging.LogRecord) -> None:
        exc = f" <- {record.exc_info[1]!r}" if record.exc_info else ""
        self.sim.current.errors.append(record.getMessage() + exc)


class DiscordSim:
    def __init__(self, bot: MathBot) -> None:
        self.bot = bot
        self.current = Reply()
        self._ids = itertools.count(1000)
        self._started = 0.0
        self._is_autocomplete = False

    # ------------------------------------------------------------------ what a user does

    async def command(self, name: str, /, *, user: int = 1, **options) -> Reply:
        """Run ``/name``; a subcommand is written ``"admin status"``."""
        return await self._interact(COMMAND, self._command_data(name, options), user=user)

    async def run(self, name: str, options: dict, **who) -> Reply:
        """Like :meth:`command`, with the options as a dict and ``roles`` / ``channel``."""
        return await self._interact(COMMAND, self._command_data(name, options), **who)

    async def autocomplete(self, name: str, option: str, text: str, **others) -> Reply:
        data = self._command_data(name, {**others, option: text})
        for item in data["options"]:
            item["focused"] = item["name"] == option
        return await self._interact(AUTOCOMPLETE, data)

    async def press(self, custom_id: str, *, message_id: str | None = None, **who) -> Reply:
        """Press a button; ``message_id`` is the message it is on (needed for the paginator)."""
        data = {"custom_id": custom_id, "component_type": BUTTON}
        return await self._interact(COMPONENT, data, message_id=message_id, **who)

    # ------------------------------------------------------------------ payloads

    @staticmethod
    def _command_data(name: str, options: dict) -> dict:
        given = [{"name": k, "type": _OPTION_TYPES[type(v)], "value": v} for k, v in options.items() if v is not None]
        for word in reversed(name.split()[1:]):
            given = [{"name": word, "type": 1, "options": given}]
        return {"id": "5", "name": name.split()[0], "type": 1, "options": given}

    async def _interact(
        self,
        kind: int,
        data: dict,
        *,
        user: int = 1,
        channel: int = CHANNEL,
        roles=(),
        message_id: str | None = None,
        dm: bool = False,
    ) -> Reply:
        self.current = reply = Reply()
        self._is_autocomplete = kind == AUTOCOMPLETE
        person = {"id": str(user), "username": f"user{user}", "discriminator": "0", "avatar": None, "global_name": None}
        payload = {
            "id": str(next(self._ids)),
            "application_id": str(APPLICATION),
            "type": kind,
            "token": "token",
            "version": 1,
            "guild_id": str(GUILD),
            "guild": {"id": str(GUILD), "locale": "en-US", "features": []},
            "channel": {"id": str(channel), "type": 0, "guild_id": str(GUILD), "name": "c", "position": 0},
            "channel_id": str(channel),
            "attachment_size_limit": 10 * 1024 * 1024,
            "entitlements": [],
            "data": data,
            "member": {
                "user": person,
                "roles": [str(r) for r in roles],
                "joined_at": "2020-01-01T00:00:00+00:00",
                "deaf": False,
                "mute": False,
                "flags": 0,
                "permissions": "0",
            },
        }
        if dm:
            del payload["guild_id"], payload["guild"], payload["member"]
            payload |= {"user": person, "channel": {"id": str(channel), "type": 1, "recipients": [person]}}
        if kind == COMPONENT:
            payload["message"] = self._message({}) | ({"id": message_id} if message_id else {})
        self._started = time.perf_counter()
        self.bot._connection.parse_interaction_create(payload)
        while waiting := [t for t in asyncio.all_tasks() if t.get_name().startswith(_TASKS) and not t.done()]:
            await asyncio.wait(waiting)
        return reply

    def _message(self, data: dict) -> dict:
        return {
            "id": str(next(self._ids)),
            "channel_id": str(CHANNEL),
            "author": {"id": str(APPLICATION), "username": "bot", "discriminator": "0", "avatar": None},
            "content": data.get("content") or "",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "components": [],
            "pinned": False,
            "type": 0,
        }

    # ------------------------------------------------------------------ Discord's side

    def _refuse(self, problems: list[str]) -> None:
        self.current.rejected += problems
        response = types.SimpleNamespace(status=400, reason="Bad Request")
        raise discord.HTTPException(response, {"code": 50035, "message": "; ".join(problems)})  # type: ignore[arg-type]

    async def request(self, route, *, payload=None, multipart=None, files=None) -> dict:
        if multipart:
            payload = json.loads(next(p["value"] for p in multipart if p["name"] == "payload_json"))
        payload = payload or {}
        has_files = bool(files)
        if not route.path.endswith("/callback"):  # a follow-up, or an edit of the original reply
            if not self.current.deferred and not self.current.messages:
                self._refuse(["follow-up before the interaction was answered"])
            if route.method == "GET":
                return self._message({})
            if problems := message_problems(payload, has_files=has_files, edit=route.method == "PATCH"):
                self._refuse(problems)
            (self.current.messages if route.method == "POST" else self.current.edits).append(payload)
            sent = self._message(payload)
            self.current.message_id = sent["id"]
            return sent

        kind, data = payload["type"], payload.get("data") or {}
        if self.current.first_reply_seconds is not None:
            self._refuse(["interaction already answered"])
        if kind == 8:
            problems = choice_problems(data["choices"])
        elif kind in (4, 7):
            problems = message_problems(data, has_files=has_files, edit=kind == 7)
        else:
            problems = []
        if self._is_autocomplete and kind != 8:
            problems.append("Value must be one of {8}: a message cannot answer an autocomplete")
        if problems:
            self._refuse(problems)
        self.current.first_reply_seconds = time.perf_counter() - self._started
        if kind == 8:
            self.current.choices = data["choices"]
        elif kind == 4:
            self.current.messages.append(data)
        elif kind == 7:
            self.current.edits.append(data)
        else:
            self.current.deferred = True
        resource = {"type": kind, "message": self._message(data)} if kind in (4, 7) else None
        return {"interaction": {"id": "1", "type": kind, "response_message_id": "2"}, "resource": resource}


@asynccontextmanager
async def discord_sim(settings, registry, monkeypatch):
    """The real bot with every cog loaded, connected to a :class:`DiscordSim`."""
    bot = MathBot(settings, registry, HealthState())
    bot._connection.application_id = APPLICATION
    me = {"id": str(APPLICATION), "username": "bot", "discriminator": "0", "avatar": None}
    bot._connection.user = discord.ClientUser(state=bot._connection, data=me)  # type: ignore[arg-type]
    role = {"color": 0, "hoist": False, "position": 1, "permissions": "0", "managed": False, "mentionable": False}
    roles = [role | {"id": str(i), "name": f"role{i}"} for i in (GUILD, ROLE)]
    bot._connection._add_guild_from_data({"id": str(GUILD), "name": "guild", "roles": roles})  # type: ignore[arg-type]
    sim = DiscordSim(bot)

    async def request(_adapter, route, session, *, payload=None, multipart=None, files=None, **_):
        return await sim.request(route, payload=payload, multipart=multipart, files=files)

    monkeypatch.setattr(AsyncWebhookAdapter, "request", request)
    handler = _ErrorLog(sim)
    logging.getLogger().addHandler(handler)
    try:
        async with bot:
            await bot.setup_hook()
            yield sim
    finally:
        logging.getLogger().removeHandler(handler)
