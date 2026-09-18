"""Building the admin DM payload, resolving the typed ID, and fanning out DMs."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any

import discord

from .config import Config, ParsedId, spaced, truncate
from .store import StateStore, Submission
from .texts import Texts

log = logging.getLogger("formbot.delivery")


@dataclass(slots=True)
class Target:
    """Where the results must be sent + what the admins should know about it."""

    user_id: int
    source: str = "self"  # self | provided | unverified
    note: str = ""

    @property
    def is_self(self) -> bool:
        return self.source == "self"


@dataclass(slots=True)
class DeliveryStats:
    delivered: list[int] = field(default_factory=list)
    forbidden: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.delivered)

    @property
    def skipped(self) -> int:
        return len(self.forbidden) + len(self.missing)


# ------------------------------------------------------------------ id resolving


async def resolve_target(
    bot: discord.Client,
    parsed: ParsedId,
    *,
    submitter: Any,
    guild: discord.Guild | None,
    texts: Texts,
) -> Target:
    submitter_id = int(getattr(submitter, "id", 0) or 0)

    if not parsed.usable:
        note = texts.tag_note.format(value=truncate(parsed.raw, 40)) if parsed.looked_like_tag else ""
        return Target(user_id=submitter_id, source="self", note=note)

    target_id = int(parsed.value)  # type: ignore[arg-type]
    if target_id == submitter_id:
        return Target(user_id=target_id, source="self")

    # Prefer the local member cache: instant and free.
    if guild is not None:
        if guild.get_member(target_id) is not None:
            return Target(user_id=target_id, source="provided")
        try:
            await guild.fetch_member(target_id)
        except discord.NotFound:
            log.info("id %s is not a member of %s — keeping it as typed, flagged", target_id, guild.name)
            return Target(
                user_id=target_id,
                source="unverified",
                note=texts.unreachable_note.format(id=spaced(target_id)),
            )
        except discord.HTTPException as exc:  # rate limited / no access: do not punish the member
            log.warning("could not verify %s in %s: %s", target_id, guild.name, exc)
            return Target(user_id=target_id, source="provided")
        return Target(user_id=target_id, source="provided")

    # Outside a guild: best effort global lookup.
    try:
        await bot.fetch_user(target_id)
    except discord.NotFound:
        return Target(user_id=target_id, source="unverified", note=texts.unreachable_note.format(id=spaced(target_id)))
    except discord.HTTPException as exc:
        log.debug("fetch_user(%s) failed: %s", target_id, exc)
    return Target(user_id=target_id, source="provided")


# ----------------------------------------------------------------------- embeds


def _code_block(text: str, limit: int) -> str:
    # Escape backticks so a member cannot break out of (or fake) the block.
    body = (text or "").replace("`", "`\u200b`")
    return f"```\n{truncate(body, limit)}\n```"


def infos_attachment(sub: Submission, *, limit: int = 4000) -> discord.File | None:
    body = (sub.infos or "")[:limit]
    if not body:
        return None
    buf = io.BytesIO(body.encode("utf-8"))
    return discord.File(buf, filename=f"form-{sub.code}.txt")  # type: ignore[arg-type]


def build_embed(
    cfg: Config,
    texts: Texts,
    sub: Submission,
    *,
    avatar_url: str | None = None,
    handled_by_name: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=texts.admin_title.format(code=sub.code),
        colour=discord.Colour(cfg.colour_int),
        timestamp=discord.utils.utcnow(),
    )

    results_line = f"<@{sub.result_user_id}> · `{spaced(sub.result_user_id)}`"
    if sub.result_source == "provided":
        results_line = f"✅ {results_line} (verified member)"
    elif sub.result_source == "unverified":
        results_line = f"⚠️ {results_line} (not in this server)"

    where = f"{sub.guild_name}" + (f" · #{sub.channel_name}" if sub.channel_name else "")
    embed.add_field(name="From", value=f"<@{sub.submitter_id}> · `{spaced(sub.submitter_id)}`", inline=True)
    embed.add_field(name="Results to", value=results_line, inline=True)
    embed.add_field(name="Submitted", value=f"<t:{int(sub.created_at)}:F>\n<t:{int(sub.created_at)}:R>", inline=True)
    embed.add_field(name="Where", value=truncate(where or "DM", 1024), inline=True)
    embed.add_field(name="Status", value=status_line(sub), inline=True)
    if sub.is_admin_origin:
        embed.add_field(name="Heads up", value=texts.admin_self_note, inline=False)

    infos = (sub.infos or "").strip()
    if len(infos) <= 1024:
        embed.add_field(name=texts.admin_infos, value=_code_block(infos, 1000) or "—", inline=False)
    else:
        preview = _code_block(infos, 500)
        embed.add_field(
            name=texts.admin_infos,
            value=f"{preview}\n*({len(infos)} chars — full text attached)*",
            inline=False,
        )

    if sub.note:
        embed.add_field(name="About that ID", value=truncate(sub.note, 1024), inline=False)
    for name, value in (sub.extra or {}).items():
        if len(embed.fields) >= 20:
            break
        embed.add_field(name=truncate(str(name), 256), value=truncate(str(value), 1024), inline=False)

    footer = (
        texts.admin_footer_handled.format(admin=handled_by_name)
        if handled_by_name
        else texts.admin_footer_open.format(count=len(sub.admin_message_ids) or len(sub.all_admin_ids))
    )
    embed.set_footer(text=truncate(footer, 600))
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def status_line(sub: Submission) -> str:
    if sub.is_handled:
        return f"🔒 handled <t:{int(sub.handled_at or 0)}:R>"
    minutes = int(sub.age_seconds // 60)
    if minutes < 1:
        return "🟢 just now"
    if minutes < 60:
        return f"🟢 open {minutes} min"
    return f"🟡 open {minutes // 60} h {minutes % 60:02d} min"


# --------------------------------------------------------------------- DM calls


async def safe_dm(bot: discord.Client, user_id: int, **kwargs: Any) -> tuple[str, discord.Message | None]:
    """DM that never raises. Returns (status, message) with status in
    delivered | forbidden | missing | error."""
    user: discord.abc.User | None = bot.get_user(user_id)
    try:
        if user is None:
            user = await bot.fetch_user(user_id)
    except discord.NotFound:
        log.warning("user id %s does not exist (check ADMIN_USER_IDS)", user_id)
        return "missing", None
    except discord.HTTPException as exc:
        log.warning("could not fetch user %s: %s", user_id, exc)
        return "error", None

    try:
        message = await user.send(**kwargs)  # type: ignore[union-attr]
    except discord.Forbidden:
        log.info("%s cannot be DM'd (DMs disabled, or they blocked the bot)", user_id)
        return "forbidden", None
    except discord.HTTPException as exc:
        log.warning("DM to %s failed: %s", user_id, str(exc)[:200])
        return "error", None
    return "delivered", message


async def deliver_to_admins(
    bot: discord.Client,
    cfg: Config,
    texts: Texts,
    store: StateStore,
    sub: Submission,
    *,
    avatar_url: str | None = None,
    view_factory: Any = None,
) -> DeliveryStats:
    """DM the form to every admin and remember each copy's message id."""
    stats = DeliveryStats()
    admins = list(store.admin_ids)
    if not admins:
        log.error("no admin IDs configured \u2014 form %s has no recipient", sub.code)
        return stats

    embed = build_embed(cfg, texts, sub, avatar_url=avatar_url)
    infos = (sub.infos or "").strip()

    for admin_id in admins:
        kwargs: dict[str, Any] = {"embed": embed}
        if len(infos) > 1024:
            attachment = infos_attachment(sub)
            if attachment is not None:
                kwargs["file"] = attachment
        if view_factory is not None:
            kwargs["view"] = view_factory(sub.code, texts)
        status, message = await safe_dm(bot, admin_id, **kwargs)
        await store.record_delivery(sub.code, admin_id=admin_id, status=status)
        if status == "delivered":
            stats.delivered.append(admin_id)
            if message is not None:
                await store.record_admin_message(sub.code, admin_id=admin_id, message_id=message.id)
        elif status == "forbidden":
            stats.forbidden.append(admin_id)
        else:
            stats.missing.append(admin_id)

    if not stats.delivered:
        log.error("form %s reached NO admin (forbidden=%s missing/error=%s)", sub.code, stats.forbidden, stats.missing)
    return stats


async def refresh_admin_copies(
    bot: discord.Client,
    cfg: Config,
    texts: Texts,
    store: StateStore,
    sub: Submission,
    *,
    acting_admin_id: int | None = None,
    handled_by_name: str | None = None,
    lock: bool = False,
) -> int:
    """Re-edit every other admin's DM copy so nobody double-handles a form."""
    updated = 0
    embed = build_embed(cfg, texts, sub, handled_by_name=handled_by_name)
    for admin_id_str, message_id in list(sub.admin_message_ids.items()):
        admin_id = int(admin_id_str)
        if acting_admin_id is not None and admin_id == int(acting_admin_id):
            continue
        try:
            user = bot.get_user(admin_id) or await bot.fetch_user(admin_id)
            channel = await user.create_dm()
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed, view=None if lock else discord.utils.MISSING)
            updated += 1
        except discord.NotFound:
            continue
        except discord.Forbidden:
            continue
        except discord.HTTPException as exc:
            log.debug("could not refresh the copy for admin %s: %s", admin_id, exc)
    return updated
