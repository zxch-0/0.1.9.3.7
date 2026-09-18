"""Every user-facing string, in English, overridable with env vars.

Centralised so the tone can be retuned without touching logic — and so the bot
stays 100 % English by default, wherever it is deployed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, get_raw, unescape


def _t(name: str, default: str) -> str:
    return unescape(get_raw(name, default)) or default


def _fallback(value: str, default: str) -> str:
    text = (value or "").strip()
    return text if len(text) <= 45 else default


@dataclass(frozen=True)
class Texts:
    # ---- the /home card
    card_footer: str
    card_dismiss: str
    card_prev: str
    card_next: str
    card_expired: str

    # ---- the modal
    modal_title: str
    field1_placeholder: str
    field1_help: str
    field2_placeholder: str
    field2_help: str
    empty_infos: str
    too_short: str
    too_long: str
    modal_expired: str

    # ---- replies to the member
    submitted: str
    no_admins: str
    nobody_reached: str
    on_cooldown: str
    contact_error: str

    # ---- the admin DM
    admin_title: str
    admin_infos: str
    admin_footer_open: str
    admin_footer_handled: str
    admin_handled_by: str
    admin_handled_noop: str
    admin_handled_self: str
    admin_self_note: str
    fallback_note: str
    tag_note: str
    invalid_note: str
    unreachable_note: str
    button_handled: str
    button_note: str
    button_hide: str
    hidden: str

    # ---- note flow
    note_title: str
    note_label: str
    note_placeholder: str
    note_to_user: str
    note_sent: str
    note_failed: str
    note_expired: str

    # ---- misc
    not_admin: str
    command_failed: str
    locked: str
    no_modal: str

    @classmethod
    def load(cls, cfg: Config) -> Texts:
        return cls(
            card_footer=_t("FORM_FOOTER", "Takes ~30 seconds · only the admin team can read what you send"),
            card_dismiss=_t("FORM_DISMISS_LABEL", "Close"),
            card_prev=_t("FORM_PREV_LABEL", "Back"),
            card_next=_t("FORM_NEXT_LABEL", "Next"),
            card_expired=_t("FORM_CARD_EXPIRED", "This card is too old to click. Run `/home` again to open a fresh one."),
            modal_title=_t("FORM_MODAL_TITLE", "Your form"),
            field1_placeholder=cfg.field1_placeholder,
            field1_help=_t("FORM_FIELD_1_HELP", "Paste it as-is — formatting is preserved."),
            field2_placeholder=cfg.field2_placeholder,
            field2_help=_t("FORM_FIELD_2_HELP", "Copy it with right-click → Copy User ID. Empty = results come back to you."),
            empty_infos=_t("FORM_EMPTY_INFOS", "You have to write something in the first box before hitting Send."),
            too_short=_t("FORM_TOO_SHORT", "That is too short — give the admins at least {min} characters to work with."),
            too_long=_t("FORM_TOO_LONG", "Too long ({length} chars). Please keep it under {limit}."),
            modal_expired=_t(
                "FORM_MODAL_EXPIRED",
                "The form window timed out before you hit Send. Nothing was sent — run `/home` again.",
            ),
            submitted=_t(
                "FORM_REPLY_SUBMITTED",
                "✅ Sent. An admin received your form `{code}` and will DM you the results shortly.\n"
                "Keep your DMs open — I can only reach you here.",
            ),
            no_admins=_t(
                "FORM_REPLY_NO_ADMINS",
                "⚠️ Your form is stored but nobody is configured to receive it. An owner needs to run `/adminform add`.",
            ),
            nobody_reached=_t(
                "FORM_REPLY_NOBODY_REACHED",
                "⚠️ Sent, but **no admin could be reached** (DMs closed, or they blocked me). "
                "Poke an admin another way — your form `{code}` is kept.",
            ),
            on_cooldown=_t("FORM_REPLY_COOLDOWN", "⏳ One form every {seconds}s — try again in **{remaining}s**."),
            contact_error=_t(
                "FORM_REPLY_ERROR",
                "⚠️ Something broke while delivering your form. It is stored as `{code}`; an admin was told. "
                "Please run `/home` again if nobody DMs you.",
            ),
            admin_title="📥 New form {code}",
            admin_infos=_fallback(cfg.field1_label, "Infos you got"),
            admin_footer_open="First admin to act wins · sent to {count} admin(s)",
            admin_footer_handled="Handled by {admin}",
            admin_handled_by="✅ Handled by {admin} — nobody else needs to act.",
            admin_handled_noop="This form was already handled by {admin}.",
            admin_handled_self="✅ Locked. {count} other admin copy(ies) were updated.",
            admin_self_note="⚠️ This form came from an admin.",
            fallback_note="Results go back to the submitter (their ID box was empty).",
            tag_note="They typed “{value}” as an ID — only a numeric user ID can be resolved, so results stay on their own ID.",
            invalid_note="⚠️ Their ID box said “{value}”, which is not a valid Discord ID. Use the submitter ID above.",
            unreachable_note="⚠️ {id} could not be resolved on Discord (wrong ID, or DMs closed).",
            button_handled=_t("FORM_BTN_HANDLED", "✅ Mark as handled"),
            button_note=_t("FORM_BTN_NOTE", "✉️ Send a note"),
            button_hide=_t("FORM_BTN_HIDE", "🗑️ Hide"),
            hidden=_t("FORM_HIDDEN", "Form handled by {admin} — this copy was hidden."),
            note_title="Note for {user}",
            note_label="Your note (sent as the bot, no admin name attached)",
            note_placeholder="Results, next step, or a question…",
            note_to_user="📩 Staff note about your form `{code}`:",
            note_sent="✅ Sent to <@{id}>.",
            note_failed="❌ Could not DM <@{id}> — DMs closed or they blocked me.",
            note_expired=_t("FORM_NOTE_EXPIRED", "That note was not sent. Reopen the DM and try again."),
            not_admin=_t("FORM_NOT_ADMIN", "Only the admin team can use these buttons."),
            command_failed=_t("FORM_COMMAND_FAILED", "⚠️ Something went wrong on my side. Nothing was sent — please try again."),
            locked=_t("FORM_LOCKED", "Already handled by {admin}."),
            no_modal=_t(
                "FORM_NO_MODAL",
                "I have no record of form `{code}` — it was sent before the last restart, so nothing can be replayed from here.",
            ),
        )
