"""The bot object: wiring, slash-command sync, and the numbers the health route shows."""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from . import views
from .config import Config
from .store import StateStore
from .texts import Texts

log = logging.getLogger("formbot.client")

EXTENSIONS = ("formbot.cog_home", "formbot.cog_admin")


class FormTree(app_commands.CommandTree):
    """Turns command errors into one-line ephemeral messages instead of tracebacks only."""

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        bot = interaction.client
        texts: Texts = getattr(bot, "texts", None) or Texts.load(Config.load())

        if isinstance(error, app_commands.CommandOnCooldown):
            remaining = max(1, int(error.retry_after + 0.999))
            await interaction.response.send_message(
                texts.on_cooldown.format(seconds=remaining, remaining=remaining), ephemeral=True
            )
            return
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            await interaction.response.send_message(texts.not_admin, ephemeral=True)
            return
        if isinstance(error, app_commands.BotMissingPermissions):
            await interaction.response.send_message(
                "⚠️ I am missing permissions in this channel (I need **Send Messages** and **Embed Links**).",
                ephemeral=True,
            )
            return

        log.exception("command %s failed: %s", getattr(error, "command", "?"), error)
        try:
            await interaction.response.send_message(texts.command_failed, ephemeral=True)
        except discord.HTTPException:
            pass  # already answered / expired


class FormBot(commands.Bot):
    def __init__(self, cfg: Config) -> None:
        # No privileged intent is required: slash commands work with the defaults and
        # member/user lookups go through the REST API.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            tree_cls=FormTree,
            allowed_mentions=discord.AllowedMentions.none(),
            case_insensitive=True,
            activity=discord.Activity(type=discord.ActivityType.watching, name="forms \u00b7 /home"),
        )
        self.cfg = cfg
        self.texts = Texts.load(cfg)
        self.store = StateStore(
            cfg.state_file,
            admins=cfg.admin_ids,
            persist=cfg.persist,
            max_stored=cfg.max_stored_submissions,
        )
        self.started_at = time.monotonic()
        self.last_ready_at: float | None = None
        self.last_gateway_error: str | None = None
        self.sync_summary: str | None = None
        self.restarts = 0

    # ------------------------------------------------------------------ startup

    async def setup_hook(self) -> None:
        await self.store.load()
        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
            except Exception as exc:  # pragma: no cover - import errors must be loud
                log.exception("could not load %s: %s", extension, exc)

        # Makes the START/handled/note buttons work on messages sent before a redeploy.
        self.add_dynamic_items(*views.dynamic_items())

        if not self.store.admin_ids:
            log.warning(
                "no admin ID configured: forms will be stored but nobody will be DM'd. "
                "Set ADMIN_USER_IDS on Render or run /adminform add @you."
            )

        if self.cfg.sync_on_start:
            self.sync_summary = await self.sync_commands()

    async def sync_commands(self) -> str:
        """Guild sync = instant; global sync = needed for /home to work in DMs."""
        parts: list[str] = []
        try:
            if self.cfg.guild_id:
                guild = discord.Object(id=self.cfg.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                parts.append(f"guild {self.cfg.guild_id}: {len(synced)} command(s)")
            synced = await self.tree.sync()
            parts.append(f"global: {len(synced)} command(s)")
        except (discord.HTTPException, discord.DiscordException) as exc:
            log.error("command sync failed: %s (check GUILD_ID and the bot's 'applications.commands' scope)", exc)
            parts.append(f"sync failed: {type(exc).__name__}")
        summary = " · ".join(parts) or "nothing to sync"
        log.info("slash commands: %s", summary)
        return summary

    # ------------------------------------------------------------------- events

    async def on_ready(self) -> None:
        self.last_ready_at = time.time()
        user = self.user
        log.info(
            "ready as %s (id %s) · %d server(s) · %d admin(s) · cooldown %ss",
            user,
            getattr(user, "id", "?"),
            len(self.guilds),
            len(self.store.admin_ids),
            self.cfg.cooldown_seconds,
        )

    async def on_resumed(self) -> None:
        log.info("gateway resumed (no data lost)")

    async def on_disconnect(self) -> None:
        self.last_gateway_error = "gateway disconnected"
        log.warning("gateway disconnected — discord.py will try to reconnect")

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        log.exception("unhandled error in %s", event_method)

    # ------------------------------------------------------------------ health

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    def health(self) -> dict[str, Any]:
        # discord.py leaves latency as NaN until the first heartbeat: NaN is not valid
        # JSON, and a monitor parsing the response strictly would choke on it.
        latency = getattr(self, "latency", None)
        gateway_ms = round(latency * 1000, 1) if latency is not None and math.isfinite(latency) and latency >= 0 else None
        return {
            "title": self.cfg.title,
            "status": "ok" if self.is_ready() else "starting",
            "ready": bool(self.is_ready()),
            "user": str(self.user) if self.user else None,
            "gateway_latency_ms": gateway_ms,
            "uptime_seconds": self.uptime_seconds,
            "servers": len(self.guilds),
            "last_gateway_error": self.last_gateway_error,
            "restarts": self.restarts,
            "slash_sync": self.sync_summary,
            **self.store.snapshot(),
        }
