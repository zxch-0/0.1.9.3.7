"""``/adminform`` — manage the team that receives the forms, without redeploying.

Useful because Render's free tier has no persistent disk: ``ADMIN_USER_IDS`` is the
source of truth restored on every boot, and these commands layer on top of it for
the current run (they are saved to ``data/state.json``, which survives a restart but
not a redeploy).
"""

from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .client import FormBot
from .delivery import safe_dm
from .store import StateStore

log = logging.getLogger("formbot.cog_admin")


def _store_of(interaction: discord.Interaction) -> StateStore:
    return getattr(interaction.client, "store", StateStore(None))


def _can_manage(interaction: discord.Interaction) -> bool:
    store = _store_of(interaction)
    user = interaction.user
    if user is None:  # pragma: no cover - defensive
        return False
    if store.is_admin(user.id):
        return True
    guild = interaction.guild
    if guild is None:
        return False
    if user.id == guild.owner_id:
        return True
    # Administrator on both sides: the member must be able to manage the bot's role too.
    me = guild.me
    perms = getattr(user, "guild_permissions", None)
    return bool(perms is not None and perms.administrator and (me is None or me.guild_permissions.administrator))


async def _require_admin(interaction: discord.Interaction) -> None:
    if not _can_manage(interaction):
        raise app_commands.MissingPermissions(["administrator"])


def _admin_text(bot: FormBot) -> str:
    store = bot.store
    if not store.admin_ids:
        return "nobody yet"
    return ", ".join(f"<@{uid}> (`{uid}`)" for uid in store.admin_ids)


class AdminForm(commands.GroupCog, name="adminform", description="Manage who receives the forms."):
    def __init__(self, bot: FormBot) -> None:
        self.bot = bot

    # -------------------------------------------------------------------- add

    @app_commands.command(name="add", description="Make a member start receiving forms")
    @app_commands.describe(user="Who should receive the forms")
    @app_commands.check(_require_admin)
    async def add(self, interaction: discord.Interaction, user: discord.User) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        added = self.bot.store.add_admin(user.id)
        status, _message = await safe_dm(interaction.client, user.id, embed=self._welcome_embed())
        note = {
            "delivered": "✅ they were DM'd the confirmation",
            "forbidden": "⚠️ they cannot be DM'd (DMs closed / bot blocked) — forms will silently fail for them",
            "missing": "❌ that user ID does not exist",
            "error": "❌ Discord refused the DM for now, try `/adminform test`",
        }.get(status, status)
        head = "➕ added to the team" if added else "ℹ️ already on the team"
        await interaction.followup.send(
            f"{head}: <@{user.id}> · {note}\n\nNow receiving forms: {_admin_text(self.bot)}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------- remove

    @app_commands.command(name="remove", description="Stop sending forms to a member")
    @app_commands.describe(user="Who should stop receiving forms")
    @app_commands.check(_require_admin)
    async def remove(self, interaction: discord.Interaction, user: discord.User) -> None:
        removed = self.bot.store.remove_admin(user.id)
        await interaction.response.send_message(
            ("➖ removed: " if removed else "ℹ️ was not on the team: ") + f"<@{user.id}>\n\n"
            + f"Now receiving forms: {_admin_text(self.bot)}",
            ephemeral=True,
        )

    # --------------------------------------------------------------------- list

    @app_commands.command(name="list", description="Show who receives the forms and the current settings")
    @app_commands.check(_require_admin)
    async def list_(self, interaction: discord.Interaction) -> None:
        store = self.bot.store
        rows = [
            f"**Admins ({len(store.admin_ids)}):**",
            _admin_text(self.bot),
            "",
            "**From env (`ADMIN_USER_IDS`):** " + (", ".join(str(a) for a in self.bot.cfg.admin_ids) or "not set"),
            f"**Forms stored:** {len(store.submissions)} ({store.open_count} open)",
            f"**Cooldown:** {self.bot.cfg.cooldown_seconds}s · **max length:** {self.bot.cfg.max_infos} chars",
        ]
        await interaction.response.send_message("\n".join(rows), ephemeral=True)

    # --------------------------------------------------------------------- test

    @app_commands.command(name="test", description="DM every admin so you know the setup works")
    @app_commands.check(_require_admin)
    async def test(self, interaction: discord.Interaction) -> None:
        store = self.bot.store
        admins = list(store.admin_ids)
        if not admins:
            await interaction.response.send_message(self.bot.texts.adminform_test_none, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = discord.Embed(
            title=self.bot.texts.test_dm_title,
            description=(
                "If you can read this, the form flow can reach you.\n\n"
                "Nothing is sent until a member runs `/home` → **START** → **Send**."
            ),
            colour=discord.Colour(self.bot.cfg.colour_int),
            timestamp=discord.utils.utcnow(),
        )
        results = []
        for admin_id in admins:
            status, _ = await safe_dm(interaction.client, admin_id, embed=embed)
            results.append(f"<@{admin_id}>: {status}")
        ok = sum(1 for entry in results if entry.endswith("delivered"))
        await interaction.followup.send(
            f"📨 {self.bot.texts.adminform_test_sent.format(count=ok, failed=len(admins) - ok)}\n" + "\n".join(results),
            ephemeral=True,
        )

    # -------------------------------------------------------------------- reset

    @app_commands.command(name="reset", description="Reload the admin list from ADMIN_USER_IDS")
    @app_commands.check(_require_admin)
    async def reset(self, interaction: discord.Interaction) -> None:
        self.bot.store.reset_admins(self.bot.cfg.admin_ids)
        await interaction.response.send_message(
            f"♻️ Admins reset from the environment: {_admin_text(self.bot)}", ephemeral=True
        )

    # --------------------------------------------------------------------- info

    @app_commands.command(name="info", description="Deployment snapshot (token is never shown)")
    @app_commands.check(_require_admin)
    async def info(self, interaction: discord.Interaction) -> None:
        payload = {**self.bot.cfg.safe_dict(), "runtime": self.bot.health()}
        body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        embed = discord.Embed(
            title="Deployment snapshot",
            description=f"```json\n{body[:3500]}\n```",
            colour=discord.Colour(self.bot.cfg.colour_int),
        )
        if len(body) > 3500:
            embed.set_footer(text="truncated — see the Render logs for the rest")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ helpers

    def _welcome_embed(self) -> discord.Embed:
        return discord.Embed(
            title=self.bot.texts.test_dm_title,
            description=(
                "You are now on the team that receives forms.\n\n"
                "When a member submits one you will get an embed with the info and the ID "
                "to answer. Buttons on that embed: **Mark as handled** (locks it for everyone), "
                "**Send a note** (DMs the member through me), **Hide** (deletes your copy)."
            ),
            colour=discord.Colour(self.bot.cfg.colour_int),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminForm(bot))
