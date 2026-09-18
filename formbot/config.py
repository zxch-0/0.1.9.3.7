"""Configuration and tiny pure helpers.

Deliberately free of ``discord.py`` imports so the parsing/validation logic can be
unit-tested (see ``tests/``) without a token or network access.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # optional, only used for local runs (Render has no .env file)
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover - python-dotenv is a soft dependency
    pass

TRUE_VALUES = {"1", "true", "yes", "on", "y"}
FALSE_VALUES = {"0", "false", "no", "off", "n", ""}

# A bare numeric ID may be wrapped in a mention and may end in the legacy discriminator.
# Mentions are unambiguous, so their length is not re-validated.
_MENTION_RE = re.compile(r"^<@!?(\d{10,25})>[.!#]?$")
_TAG_RE = re.compile(r"^@?[A-Za-z0-9_.]{2,32}(?:#\d{2,5})?$")
_DIGITS_RE = re.compile(r"^\d+$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,24}$")


# --------------------------------------------------------------------------- env


def get_raw(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def get_bool(name: str, default: bool = True) -> bool:
    raw = get_raw(name)
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    return default


def get_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = get_raw(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def unescape(text: str) -> str:
    """Allow ``\\n`` inside env vars — Render's dashboard has no multiline field."""
    return (text or "").replace("\\n", "\n").replace("\\t", "\t")


def parse_id_list(raw: str) -> tuple[int, ...]:
    """Accept "1, 2 3;4" or a bare snowflake; return a deduplicated int tuple."""
    out: list[int] = []
    for chunk in re.split(r"[\s,;]+", raw or ""):
        chunk = chunk.strip("<@!> ")
        if not chunk or not _DIGITS_RE.match(chunk):
            continue
        value = int(chunk)
        if value and value not in out:
            out.append(value)
    return tuple(out)


def parse_pages(count: int, title_env: str, body_env: str, default_title: str) -> tuple[tuple[str, str], ...]:
    """Read PAGE2_*/PAGE3_* style env vars into ``[(title, body), ...]`` (page 1 first)."""
    pages: list[tuple[str, str]] = [("", "")]
    for index in range(2, max(2, count + 1) + 1):
        title = get_raw(f"{title_env}{index}")
        body = unescape(get_raw(f"{body_env}{index}"))
        if title or body:
            pages.append((title or default_title, body))
    if len(pages) == 1:
        return (("", ""),)
    pages[0] = (default_title, "")  # page 1 body is the main FORM_BLURB
    return tuple(pages)


# --------------------------------------------------------------------- id parsing


@dataclass(frozen=True)
class ParsedId:
    """Outcome of reading the free-text "your Discord ID" box.

    ``kind`` is one of:
      ``empty``   -> nothing typed
      ``digits``  -> a bare numeric snowflake
      ``mention`` -> ``<@123>`` / ``<@!123>``
      ``invalid`` -> digits of the wrong length (a phone number, most likely)
      ``tag``     -> ``name`` / ``@name`` / ``name#1234`` -> cannot be resolved
    """

    raw: str = ""
    kind: str = "empty"
    value: int | None = None

    @property
    def usable(self) -> bool:
        return self.value is not None

    @property
    def looked_like_tag(self) -> bool:
        return self.kind == "tag"


def parse_discord_id(raw: Any) -> ParsedId:
    text = "" if raw is None else str(raw).strip()
    if not text:
        return ParsedId(raw=text, kind="empty")

    mention = _MENTION_RE.match(text)
    if mention:
        return ParsedId(raw=text, kind="mention", value=int(mention.group(1)))

    candidate = text.lstrip("@").strip()
    if _DIGITS_RE.match(candidate):
        # Discord snowflakes are uint64: 15..20 digits in practice.
        if 15 <= len(candidate) <= 20:
            return ParsedId(raw=text, kind="digits", value=int(candidate))
        return ParsedId(raw=text, kind="invalid")

    if _TAG_RE.match(candidate):
        return ParsedId(raw=text, kind="tag")

    return ParsedId(raw=text, kind="invalid")


def discord_epoch(seconds: float | None = None) -> int:
    """Unix timestamp, for Discord's ``<t:...>`` timestamps."""
    import time

    return int(time.time() if seconds is None else seconds)


# ---------------------------------------------------------------------- display


def spaced(value: int) -> str:
    """123456789012345678 -> 1 234 567 890 123 456 78 (much easier to read)."""
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def mask(text: str, *secrets: str | None) -> str:
    """Keep tokens out of logs and out of admin-visible error messages."""
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[redacted]")
    return out


def format_env_value(value: str, keep: int = 2) -> str:
    """Describe a secret without ever printing it in full (used by /adminform info)."""
    if not value:
        return "(not set)"
    if len(value) <= keep * 2:
        return f"set ({len(value)} chars)"
    return f"{value[:keep]}…{value[-keep:]} ({len(value)} chars)"


# ------------------------------------------------------------------ form fields


@dataclass(frozen=True)
class FieldSpec:
    """One extra modal box, declared as ``key:Label:style:required:max_length``."""

    key: str
    label: str
    style: str = "short"  # short | long
    required: bool = False
    max_length: int = 200
    placeholder: str = ""

    @property
    def multiline(self) -> bool:
        return self.style == "long"

    @classmethod
    def parse(cls, raw: str) -> FieldSpec | None:
        parts = [chunk.strip() for chunk in (raw or "").split(":")]
        key = (parts[0] or "").replace(" ", "_").replace("-", "_")
        if not _KEY_RE.match(key or ""):
            return None
        label = parts[1] if len(parts) > 1 and parts[1] else key.replace("_", " ").title()
        style = parts[2].lower() if len(parts) > 2 and parts[2] else "short"
        if style not in {"short", "long"}:
            style = "short"
        required = get_bool_literal(parts[3]) if len(parts) > 3 and parts[3] else False
        try:
            max_length = int(parts[4]) if len(parts) > 4 and parts[4] else (1000 if style == "long" else 200)
        except ValueError:
            max_length = 1000 if style == "long" else 200
        max_length = max(1, min(max_length, 4000))
        placeholder = ":".join(parts[5:]).strip()[:100] if len(parts) > 5 else ""
        return cls(key=key, label=label[:45], style=style, required=required, max_length=max_length, placeholder=placeholder)


def parse_fieldset(raw: str) -> tuple[FieldSpec, ...]:
    out: list[FieldSpec] = []
    for chunk in re.split(r"[;,\n]+", raw or ""):
        spec = FieldSpec.parse(chunk)
        if spec is not None and spec.key not in {f.key for f in out}:
            out.append(spec)
    return tuple(out)


def get_bool_literal(raw: str | None) -> bool:
    return (raw or "").strip().lower() in TRUE_VALUES


def parse_page_titles(count: int, prefix: str) -> tuple[str, ...]:
    titles: list[str] = []
    for index in range(2, max(2, count + 1)):
        title = get_raw(f"{prefix}{index}")
        if title:
            titles.append(title)
    return tuple(titles)


# ----------------------------------------------------------------------- config

DEFAULT_BLURB = (
    "Press **START**, paste the info you have, drop your Discord ID.\n"
    "An admin picks it up and DMs you the results here on Discord."
)


@dataclass(frozen=True)
class Config:
    # credentials / targeting
    token: str = ""
    admin_ids: tuple[int, ...] = ()
    guild_id: int | None = None

    # card appearance
    title: str = "Intake form"
    headline: str = "Need something handled?"
    blurb: str = ""
    start_label: str = "START"
    start_emoji: str = "\u25b6\ufe0f"
    colour: str = "5865F2"
    banner_url: str = ""
    page_titles: tuple[str, ...] = ()
    page_fields: tuple[tuple[FieldSpec, ...], ...] = ()

    # modal fields (page 1)
    field1_label: str = "Infos you got"
    field1_placeholder: str = "Everything you have: what you need, links, names, deadlines\u2026"
    field2_label: str = "Your Discord ID to send u results"
    field2_placeholder: str = "123456789012345678  or  leave empty to use your own ID"
    max_infos: int = 2000
    min_infos: int = 3
    max_id_chars: int = 60

    # behaviour
    cooldown_seconds: int = 30
    modal_timeout_minutes: int = 15
    notify_on_handled: bool = True
    sync_on_start: bool = True

    # state
    data_dir: Path = field(default_factory=lambda: Path("data"))
    persist: bool = True
    max_stored_submissions: int = 200

    # keep-alive / health
    health_enabled: bool = True
    port: int = 10000
    ping_token: str = ""
    log_level: str = "info"

    @classmethod
    def load(cls) -> Config:
        admins = parse_id_list(get_raw("ADMIN_USER_IDS"))
        guild = parse_id_list(get_raw("GUILD_ID"))
        title = get_raw("FORM_TITLE", "Intake form") or "Intake form"
        emoji = get_raw("FORM_START_EMOJI", "\u25b6\ufe0f")
        if emoji.lower() in {"none", "off", "-"}:
            emoji = ""
        page_count = get_int("FORM_PAGES", 1, minimum=1, maximum=5)
        return cls(
            token=get_raw("DISCORD_TOKEN") or get_raw("DISCORD_BOT_TOKEN"),
            admin_ids=admins,
            guild_id=guild[0] if guild else None,
            title=title,
            headline=unescape(get_raw("FORM_HEADLINE")) or "Need something handled?",
            blurb=unescape(get_raw("FORM_BLURB")),
            start_label=(get_raw("FORM_START_LABEL", "START") or "START")[:70],
            start_emoji=emoji,
            colour=(get_raw("EMBED_COLOUR", "5865F2") or "5865F2").lstrip("#"),
            banner_url=get_raw("FORM_BANNER_URL"),
            page_titles=parse_page_titles(page_count, "FORM_PAGE"),
            page_fields=tuple(
                parse_fieldset(unescape(get_raw(f"FORM_FIELDS_P{index}")))
                for index in range(2, max(2, page_count + 1))
            ),
            field1_label=unescape(get_raw("FORM_FIELD_1_LABEL")) or "Infos you got",
            field1_placeholder=unescape(get_raw("FORM_FIELD_1_PLACEHOLDER"))
            or "Everything you have: what you need, links, names, deadlines\u2026",
            field2_label=unescape(get_raw("FORM_FIELD_2_LABEL")) or "Your Discord ID to send u results",
            field2_placeholder=unescape(get_raw("FORM_FIELD_2_PLACEHOLDER"))
            or "123456789012345678  or  leave empty to use your own ID",
            max_infos=get_int("FORM_MAX_INFOS", 2000, minimum=100, maximum=4000),
            min_infos=get_int("FORM_MIN_INFOS", 3, minimum=1, maximum=500),
            max_id_chars=get_int("FORM_MAX_ID_CHARS", 60, minimum=20, maximum=4000),
            cooldown_seconds=get_int("FORM_COOLDOWN_SECONDS", 30, minimum=0, maximum=86_400),
            modal_timeout_minutes=get_int("FORM_MODAL_TIMEOUT_MINUTES", 15, minimum=1, maximum=60),
            notify_on_handled=get_bool("NOTIFY_ON_HANDLED", True),
            sync_on_start=get_bool("SYNC_ON_START", True),
            data_dir=Path(get_raw("DATA_DIR", "data") or "data"),
            persist=get_bool("PERSIST_STATE", True),
            max_stored_submissions=get_int("MAX_STORED_SUBMISSIONS", 200, minimum=10, maximum=5000),
            health_enabled=get_bool("HEALTH_ENABLED", True),
            port=get_int("PORT", 10000, minimum=1, maximum=65_535),
            ping_token=get_raw("PING_TOKEN"),
            log_level=(get_raw("LOG_LEVEL", "info") or "info").lower(),
        )

    # ------------------------------------------------------------------ helpers

    @property
    def token_present(self) -> bool:
        return bool(self.token)

    @property
    def colour_int(self) -> int:
        try:
            return int(self.colour, 16) & 0xFFFFFF
        except ValueError:
            return 0x5865F2

    @property
    def main_body(self) -> str:
        body = self.blurb or DEFAULT_BLURB
        return f"### {self.headline}\n\n{body}" if self.headline else body

    @property
    def card_pages(self) -> tuple[tuple[str, str], ...]:
        """``[(embed title, embed description), ...]`` — page 1 is the real card."""
        pages: list[tuple[str, str]] = [(self.title, self.main_body)]
        for offset, heading in enumerate(self.page_titles):
            fields = self.page_fields[offset] if offset < len(self.page_fields) else ()
            hint = "\n".join(f"• {f.label}" for f in fields)
            pages.append((heading, hint or self.main_body))
        return tuple(pages)

    @property
    def page_count(self) -> int:
        return len(self.card_pages)

    @property
    def multi_page(self) -> bool:
        return self.page_count > 1

    def fields_for_page(self, page: int) -> tuple[FieldSpec, ...]:
        if page <= 0:
            return ()
        offset = page - 1
        return self.page_fields[offset] if offset < len(self.page_fields) else ()

    def label_for(self, key: str, page: int) -> str:
        for spec in self.fields_for_page(page):
            if spec.key == key:
                return spec.label
        return key.replace("_", " ").title()

    @property
    def start_button_label(self) -> str:
        parts = [p for p in (self.start_emoji, self.start_label) if p]
        return "  ".join(parts)[:80]

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    def safe_dict(self) -> dict[str, Any]:
        """Config snapshot for ``/home info`` — the token is never included."""
        return {
            "title": self.title,
            "admins_from_env": list(self.admin_ids),
            "guild_id": self.guild_id,
            "pages": self.page_count,
            "cooldown_seconds": self.cooldown_seconds,
            "max_infos": self.max_infos,
            "min_infos": self.min_infos,
            "modal_timeout_minutes": self.modal_timeout_minutes,
            "notify_on_handled": self.notify_on_handled,
            "persist": self.persist,
            "data_dir": str(self.data_dir),
            "health_enabled": self.health_enabled,
            "port": self.port,
            "log_level": self.log_level,
            "token": format_env_value(self.token),
        }
