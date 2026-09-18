"""All Discord UI: the ``/home`` card with the big START button, the form modal,
and the admin DM copy with its action buttons.

Design notes
------------
* Every button is a :class:`discord.ui.DynamicItem` with a stable ``custom_id``,
  so cards and admin DMs keep working after a redeploy with no ``bot.add_view``
  bookkeeping (the state — page number, form code — lives inside the custom_id).
* The member only ever sees ephemeral messages; the form goes to admin DMs.
* Nothing is trusted from Discord that :class:`StateStore` does not already know.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import discord

from .config import Config, FieldSpec, parse_discord_id, spaced, truncate
from .delivery import build_embed, deliver_to_admins, refresh_admin_copies, resolve_target, safe_dm
from .store import StateStore
from .texts import Texts

log = logging.getLogger("formbot.views")

_CODE = r"[A-Z0-9]{4,10}"
TPL_START = r"^fb:start:(?P<page>\d+)$"
TPL_PREV = r"^fb:prev:(?P<page>\d+)$"
TPL_NEXT = r"^fb:next:(?P<page>\d+)$"
TPL_CLOSE = r"^fb:close$"
TPL_HANDLED = rf"^fb:handled:(?P<code>{_CODE})$"
TPL_NOTE = rf"^fb:note:(?P<code>{_CODE})$"
TPL_HIDE = rf"^fb:hide:(?P<code>{_CODE})$"


# ------------------------------------------------------------- client accessors


def _client(interaction: discord.Interaction) -> Any:
    return interaction.client


def _cfg(interaction: discord.Interaction) -> Config:
    return getattr(_client(interaction), "cfg", None) or Config.load()


def _texts(interaction: discord.Interaction) -> Texts:
    texts = getattr(_client(interaction), "texts", None)
    return texts if texts is not None else Texts.load(_cfg(interaction))


def _store(interaction: discord.Interaction) -> StateStore:
    store = getattr(_client(interaction), "store", None)
    return store if store is not None else StateStore(None)


def _avatar(client: Any) -> str | None:
    user = getattr(client, "user", None)
    if user is None:
        return None
    try:
        return str(user.display_avatar.url)
    except Exception:  # pragma: no cover - avatar is cosmetic
        return None


async def _safe_reply(interaction: discord.Interaction, content: str) -> None:
    """Reply without ever raising: the interaction may already be expired."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, ephemeral=True)
        else:
            await interaction.response.send_message(content=content, ephemeral=True)
    except Exception as exc:  # the interaction is gone: nothing sane left to do
        log.debug("could not answer a stale interaction: %s", exc)


def _code_of(custom_id: str) -> str:
    return custom_id.rsplit(":", 1)[-1]


def _page_of(custom_id: str) -> int:
    tail = custom_id.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


# ---------------------------------------------------------------------- the card


def home_embed(cfg: Config, texts: Texts, *, page: int, avatar_url: str | None = None) -> discord.Embed:
    pages = cfg.card_pages
    page = max(0, min(page, len(pages) - 1))
    title, body = pages[page]
    embed = discord.Embed(
        title=truncate(title, 256),
        description=truncate(body or cfg.main_body, 4000),
        colour=discord.Colour(cfg.colour_int),
    )
    if len(pages) > 1:
        embed.set_author(name=f"Page {page + 1} of {len(pages)}")
    embed.set_footer(text=truncate(texts.card_footer, 600))
    if cfg.banner_url:
        embed.set_image(url=cfg.banner_url)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


class StartItem(discord.ui.DynamicItem[discord.ui.Button], template=TPL_START):
    """The big green START button — alone on its row, so it stretches as wide as
    Discord allows."""

    def __init__(self, *, page: int, label: str, emoji: str | None = None, disabled: bool = False) -> None:
        kwargs: dict[str, Any] = {
            "label": truncate(label, 80),
            "style": discord.ButtonStyle.success,
            "disabled": disabled,
            "custom_id": f"fb:start:{page}",
        }
        if emoji:
            kwargs["emoji"] = emoji
        super().__init__(discord.ui.Button(**kwargs), row=0)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: Any, /) -> StartItem:
        cfg = _cfg(interaction)
        return cls(page=int(match.group("page")), label=cfg.start_label, emoji=cfg.start_emoji or None)

    async def callback(self, interaction: discord.Interaction) -> None:
        cfg, texts, store = _cfg(interaction), _texts(interaction), _store(interaction)
        page = _page_of(self.custom_id)

        remaining = store.cooldown_remaining(interaction.user.id, cfg.cooldown_seconds)
        if remaining:
            await interaction.response.send_message(
                texts.on_cooldown.format(seconds=cfg.cooldown_seconds, remaining=remaining), ephemeral=True
            )
            return

        try:
            await interaction.response.send_modal(FormModal(cfg=cfg, texts=texts, page=page))
        except discord.HTTPException as exc:
            log.warning("could not open the form modal: %s", exc)
            await _safe_reply(interaction, texts.card_expired)


class _NavMixin:
    step = 0

    def __init__(self, *, page: int, label: str, disabled: bool = False) -> None:
        name = "next" if self.step > 0 else "prev"
        button = discord.ui.Button(
            label=truncate(label, 80),
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            custom_id=f"fb:{name}:{page}",
        )
        discord.ui.DynamicItem.__init__(self, button, row=1)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: Any, /) -> Any:
        texts = _texts(interaction)
        label = texts.card_next if cls.step > 0 else texts.card_prev
        return cls(page=int(match.group("page")), label=label)

    async def callback(self, interaction: discord.Interaction) -> None:
        cfg, texts = _cfg(interaction), _texts(interaction)
        total = cfg.page_count
        page = _page_of(self.custom_id)  # type: ignore[attr-defined]
        target = max(0, min(total - 1, page + self.step))
        client = _client(interaction)
        await interaction.response.edit_message(
            embed=home_embed(cfg, texts, page=target, avatar_url=_avatar(client)),
            view=home_view(cfg, texts, page=target, avatar_url=_avatar(client)),
        )


class NextItem(_NavMixin, discord.ui.DynamicItem[discord.ui.Button], template=TPL_NEXT):
    step = 1


class PrevItem(_NavMixin, discord.ui.DynamicItem[discord.ui.Button], template=TPL_PREV):
    step = -1


class CloseItem(discord.ui.DynamicItem[discord.ui.Button], template=TPL_CLOSE):
    def __init__(self, *, label: str) -> None:
        button = discord.ui.Button(
            label=truncate(label, 80), style=discord.ButtonStyle.secondary, custom_id="fb:close"
        )
        super().__init__(button, row=1)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: Any, /) -> CloseItem:
        return cls(label=_texts(interaction).card_dismiss)

    async def callback(self, interaction: discord.Interaction) -> None:
        # discord.py >= 2.x: deleting the ephemeral original response closes the card.
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.debug("close: the card is already gone")


def home_view(cfg: Config, texts: Texts, *, page: int = 0, avatar_url: str | None = None) -> discord.ui.View:
    total = cfg.page_count
    view = discord.ui.View(timeout=None)
    label = f"{page + 1}/{total} · {cfg.start_label}" if total > 1 else cfg.start_label
    view.add_item(StartItem(page=page, label=label, emoji=cfg.start_emoji or None))
    if total > 1:
        view.add_item(PrevItem(page=page, label=texts.card_prev, disabled=page <= 0))
        view.add_item(NextItem(page=page, label=texts.card_next, disabled=page >= total - 1))
    view.add_item(CloseItem(label=texts.card_dismiss))
    return view


# -------------------------------------------------------------------- the modal


def default_fields(cfg: Config) -> list[FieldSpec]:
    """The two boxes the flow asks for: the info, then where results should go."""
    return [
        FieldSpec(
            key="infos",
            label=truncate(cfg.field1_label, 45),
            style="long",
            required=True,
            max_length=cfg.max_infos,
            placeholder=cfg.field1_placeholder,
        ),
        FieldSpec(
            key="result_id",
            label=truncate(cfg.field2_label, 45),
            style="short",
            required=False,
            max_length=cfg.max_id_chars,
            placeholder=cfg.field2_placeholder,
        ),
    ]


def modal_fields(cfg: Config, page: int) -> list[FieldSpec]:
    return default_fields(cfg) if page <= 0 else list(cfg.fields_for_page(page))


class FormModal(discord.ui.Modal):
    """The form itself. Built dynamically, so extra pages need no extra code."""

    def __init__(self, *, cfg: Config, texts: Texts, page: int = 0) -> None:
        title = texts.modal_title if page <= 0 else f"{texts.modal_title} · {page + 1}/{cfg.page_count}"
        super().__init__(title=truncate(title, 45), timeout=cfg.modal_timeout_minutes * 60)
        self.cfg = cfg
        self.texts = texts
        self.page = page
        self.fields: dict[str, discord.ui.TextInput] = {}

        for spec in modal_fields(cfg, page):
            kwargs: dict[str, Any] = {
                "style": discord.TextStyle.paragraph if spec.multiline else discord.TextStyle.short,
                "required": spec.required,
                "max_length": max(1, min(spec.max_length, 4000)),
                "custom_id": f"fb:in:{page}:{spec.key}",
            }
            # `label` is only accepted in the constructor (the property is deprecated),
            # so every string is clamped to Discord's 45-char limit up front.
            kwargs["label"] = truncate(spec.label, 45) or "Answer"
            if spec.placeholder:
                kwargs["placeholder"] = truncate(spec.placeholder, 100)
            if spec.key == "infos" and cfg.min_infos > 1:
                kwargs["min_length"] = min(cfg.min_infos, kwargs["max_length"])
            field = discord.ui.TextInput(**kwargs)
            self.fields[spec.key] = field
            self.add_item(field)

    def value_of(self, key: str) -> str:
        field = self.fields.get(key)
        return (field.value or "").strip() if field is not None else ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg, texts, store = _cfg(interaction), _texts(interaction), _store(interaction)
        client = _client(interaction)

        if self.page <= 0:
            infos = self.value_of("infos")
            if not infos:
                await interaction.response.send_message(texts.empty_infos, ephemeral=True)
                return
            if len(infos) < cfg.min_infos:
                await interaction.response.send_message(texts.too_short.format(min=cfg.min_infos), ephemeral=True)
                return
            if len(infos) > cfg.max_infos:
                await interaction.response.send_message(
                    texts.too_long.format(length=len(infos), limit=cfg.max_infos), ephemeral=True
                )
                return
            raw_id = self.value_of("result_id")
        else:
            # Extra pages: the "infos" box is whatever the first required field says.
            infos = self.value_of("infos") or next((self.value_of(k) for k in self.fields if self.value_of(k)), "")
            if not infos:
                await interaction.response.send_message(texts.empty_infos, ephemeral=True)
                return
            raw_id = ""

        # Second cooldown check: a member can keep two modals open at once.
        remaining = store.cooldown_remaining(interaction.user.id, cfg.cooldown_seconds)
        if remaining:
            await interaction.response.send_message(
                texts.on_cooldown.format(seconds=cfg.cooldown_seconds, remaining=remaining), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        store.touch(interaction.user.id)

        extra: dict[str, str] = {}
        if self.page > 0:
            for key, field in self.fields.items():
                answer = (field.value or "").strip()
                if answer and key != "infos":
                    extra[truncate(cfg.label_for(key, self.page), 64)] = truncate(answer, 900)

        parsed = parse_discord_id(raw_id)
        target = await resolve_target(client, parsed, submitter=interaction.user, guild=interaction.guild, texts=texts)

        if parsed.kind == "invalid":
            target.note = target.note or texts.invalid_note.format(value=truncate(parsed.raw, 40))
        if parsed.usable and target.source == "unverified":
            # A wrong/foreign ID must not swallow the submission: results stay with
            # the submitter, and the typed value is still shown to the admins.
            target.note = (target.note + "\n" + texts.fallback_note).strip()
            target.user_id = interaction.user.id
            target.source = "self"

        channel = interaction.channel
        admins_configured = list(store.admin_ids)
        sub = await store.create(
            submitter_id=interaction.user.id,
            submitter_tag=getattr(interaction.user, "display_name", str(interaction.user)),
            result_user_id=target.user_id,
            result_source=target.source,
            infos=infos[: cfg.max_infos],
            note=target.note,
            page=self.page,
            extra=extra,
            guild_id=interaction.guild.id if interaction.guild is not None else None,
            guild_name=interaction.guild.name if interaction.guild is not None else "DM",
            channel_id=getattr(channel, "id", None),
            channel_name=getattr(channel, "name", "") or "direct-message",
            is_admin_origin=store.is_admin(interaction.user.id),
            all_admin_ids=admins_configured,
        )

        if not admins_configured:
            log.error("form %s stored but nobody is configured to receive it", sub.code)
            await interaction.followup.send(content=texts.no_admins, ephemeral=True)
            return

        try:
            stats = await deliver_to_admins(
                client, cfg, texts, store, sub, avatar_url=_avatar(client), view_factory=admin_view
            )
        except Exception:  # a delivery crash must never eat the member's form
            log.exception("delivery failed for form %s", sub.code)
            await interaction.followup.send(content=texts.contact_error.format(code=sub.code), ephemeral=True)
            return

        if stats.count == 0:
            await interaction.followup.send(content=texts.nobody_reached.format(code=sub.code), ephemeral=True)
            return

        await interaction.followup.send(content=texts.submitted.format(code=sub.code, admins=stats.count), ephemeral=True)
        log.info("form %s from %s delivered to %d admin(s)", sub.code, sub.submitter_tag, stats.count)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.warning("form modal failed: %s", error)
        await _safe_reply(interaction, _texts(interaction).modal_expired)


# ---------------------------------------------------------- admin DM interactions


class HandledItem(discord.ui.DynamicItem[discord.ui.Button], template=TPL_HANDLED):
    """The anti-double-work lock: first admin to click wins, everyone else is locked."""

    def __init__(self, *, code: str, label: str, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label=truncate(label, 80),
                style=discord.ButtonStyle.success,
                disabled=disabled,
                custom_id=f"fb:handled:{code}",
            ),
            row=0,
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: Any, /) -> HandledItem:
        return cls(code=match.group("code"), label=_texts(interaction).button_handled)

    async def callback(self, interaction: discord.Interaction) -> None:
        cfg, texts, store = _cfg(interaction), _texts(interaction), _store(interaction)
        client = _client(interaction)

        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(texts.not_admin, ephemeral=True)
            return

        code = _code_of(self.custom_id)
        sub, won = await store.claim(code, interaction.user.id)
        if sub is None:
            # Nothing in the store: the bot was redeployed (the free tier has no disk)
            # or somebody tampered with the button. Explain instead of erroring forever.
            await interaction.response.send_message(texts.no_modal.format(code=code), ephemeral=True)
            return

        actor = getattr(interaction.user, "display_name", None) or spaced(interaction.user.id)

        if not won:
            who = f"<@{sub.handled_by}>" if sub.handled_by else "another admin"
            embed = build_embed(cfg, texts, sub, handled_by_name=actor)
            try:
                await interaction.response.edit_message(embed=embed, view=None)
            except discord.HTTPException:
                await interaction.response.send_message(texts.locked.format(admin=who), ephemeral=True)
            return

        embed = build_embed(cfg, texts, sub, handled_by_name=actor)
        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except discord.HTTPException as exc:
            log.debug("could not edit my own copy: %s", exc)

        locked = await refresh_admin_copies(
            client,
            cfg,
            texts,
            store,
            sub,
            acting_admin_id=interaction.user.id,
            handled_by_name=actor,
            lock=True,
        )

        if cfg.notify_on_handled and not sub.is_admin_origin:
            heads_up = discord.Embed(
                title=f"Your form {sub.code}",
                description="An admin marked it as handled. If the results were sent to you in DM, you are all set.",
                colour=discord.Colour(0x57F287),
            )
            await safe_dm(client, sub.result_user_id, embed=heads_up)

        await interaction.followup.send(content=texts.admin_handled_self.format(count=locked), ephemeral=True)


class NoteItem(discord.ui.DynamicItem[discord.ui.Button], template=TPL_NOTE):
    def __init__(self, *, code: str, label: str) -> None:
        super().__init__(
            discord.ui.Button(label=truncate(label, 80), style=discord.ButtonStyle.primary, custom_id=f"fb:note:{code}"),
            row=0,
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: Any, /) -> NoteItem:
        return cls(code=match.group("code"), label=_texts(interaction).button_note)

    async def callback(self, interaction: discord.Interaction) -> None:
        texts, store = _texts(interaction), _store(interaction)
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(texts.not_admin, ephemeral=True)
            return
        code = _code_of(self.custom_id)
        sub = store.get(code)
        if sub is None:
            await interaction.response.send_message(texts.no_modal.format(code=code), ephemeral=True)
            return
        modal = NoteModal(code=code, texts=texts, target_name=sub.submitter_tag or spaced(sub.result_user_id))
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException as exc:
            log.debug("note modal refused: %s", exc)
            await _safe_reply(interaction, texts.note_expired)


class HideItem(discord.ui.DynamicItem[discord.ui.Button], template=TPL_HIDE):
    """Removes the DM copy for the admin who clicked it (nobody else is affected)."""

    def __init__(self, *, code: str, label: str) -> None:
        super().__init__(
            discord.ui.Button(label=truncate(label, 80), style=discord.ButtonStyle.danger, custom_id=f"fb:hide:{code}"),
            row=0,
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Any, match: Any, /) -> HideItem:
        return cls(code=match.group("code"), label=_texts(interaction).button_hide)

    async def callback(self, interaction: discord.Interaction) -> None:
        texts, store = _texts(interaction), _store(interaction)
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(texts.not_admin, ephemeral=True)
            return
        code = _code_of(self.custom_id)
        who = getattr(interaction.user, "display_name", "an admin")
        try:
            await interaction.response.send_message(texts.hidden.format(admin=who), ephemeral=True)
        except discord.HTTPException:
            log.debug("hide: reply not needed")
        if interaction.message is not None:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                log.debug("hide: the copy is already gone")
        sub = store.get(code)
        if sub is not None:
            await store.record_delivery(sub.code, admin_id=interaction.user.id, status="hidden")


class NoteModal(discord.ui.Modal):
    """Lets an admin answer the member *through* the bot, without exposing their profile."""

    def __init__(self, *, code: str, texts: Texts, target_name: str) -> None:
        super().__init__(title=truncate(texts.note_title.format(user=target_name), 45), timeout=15 * 60)
        self.code = code
        self.texts = texts
        self.note = discord.ui.TextInput(
            label=truncate(texts.note_label, 45),
            style=discord.TextStyle.paragraph,
            placeholder=truncate(texts.note_placeholder, 100),
            required=True,
            min_length=2,
            max_length=1500,
            custom_id="fb:in:note",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg, texts, store = _cfg(interaction), _texts(interaction), _store(interaction)
        client = _client(interaction)

        body = (self.note.value or "").strip()
        if not body:
            await interaction.response.send_message(texts.empty_infos, ephemeral=True)
            return
        if not store.is_admin(interaction.user.id):
            await interaction.response.send_message(texts.not_admin, ephemeral=True)
            return
        sub = store.get(self.code)
        if sub is None:
            await interaction.response.send_message(texts.no_modal.format(code=self.code), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        embed = discord.Embed(
            title=texts.note_to_user.format(code=sub.code),
            description=truncate(body, 4000),
            colour=discord.Colour(cfg.colour_int),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Reply to this DM and the staff team will see it.")
        status, _message = await safe_dm(client, sub.result_user_id, embed=embed)
        delivered = status == "delivered"
        await store.record_note(self.code, admin_id=interaction.user.id, text=body, delivered=delivered)

        template = texts.note_sent if delivered else texts.note_failed
        await interaction.followup.send(content=template.format(id=sub.result_user_id), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.warning("note modal failed: %s", error)
        await _safe_reply(interaction, _texts(interaction).note_expired)


def admin_view(code: str, texts: Texts) -> discord.ui.View:
    """The three buttons attached to each admin's DM copy."""
    view = discord.ui.View(timeout=None)
    view.add_item(HandledItem(code=code, label=texts.button_handled))
    view.add_item(NoteItem(code=code, label=texts.button_note))
    view.add_item(HideItem(code=code, label=texts.button_hide))
    return view


def dynamic_items() -> Sequence[Any]:
    """Registered at startup so messages survive a redeploy."""
    return (StartItem, NextItem, PrevItem, CloseItem, HandledItem, NoteItem, HideItem)
