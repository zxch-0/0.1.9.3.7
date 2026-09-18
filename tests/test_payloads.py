"""Payload linting: everything Discord's API validates server-side, checked offline.

These are the bugs that only ever show up in production ("400 Invalid Form Body",
buttons that silently stop dispatching), so they are worth asserting.
"""

from __future__ import annotations

import re

import discord
import pytest

from formbot.config import Config
from formbot.delivery import build_embed
from formbot.store import Submission
from formbot.texts import Texts
from formbot.views import FormModal, admin_view, default_fields, dynamic_items, home_view

EMBED_LIMITS = {"title": 256, "description": 4096, "fields": 25, "field_name": 256, "field_value": 1024, "footer": 600}


def cfg(**env: str) -> Config:
    import os

    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(dict(env))
    try:
        return Config.load()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ------------------------------------------------------------------- modals


@pytest.mark.parametrize(
    "config",
    [
        cfg(),
        cfg(FORM_FIELD_1_LABEL="x" * 200, FORM_FIELD_2_LABEL="", FORM_MAX_INFOS="4000", FORM_MIN_INFOS="5"),
        cfg(FORM_PAGES="5", FORM_PAGE2="P2", FORM_FIELDS_P2="a:A:long:yes:4000;b:B;c:C:short:no:500", FORM_PAGE3="P3", FORM_FIELDS_P3="d:D"),
    ],
)
def test_modal_payload_is_valid(config):
    for page in range(config.page_count):
        modal = FormModal(cfg=config, texts=Texts.load(config), page=page)
        assert 1 <= len(modal.children) <= 5, "a modal accepts between 1 and 5 rows"
        assert 1 <= len(modal.title) <= 45
        for field in modal.children:
            assert 1 <= len(field._underlying.label or "") <= 45
            assert field._underlying.custom_id and len(field._underlying.custom_id) <= 100
            placeholder = field._underlying.placeholder
            assert placeholder is None or len(placeholder) <= 100
            assert field._underlying.max_length and 1 <= field._underlying.max_length <= 4000
            if field._underlying.min_length:
                assert field._underlying.min_length <= field._underlying.max_length


def test_infos_box_is_required_and_id_box_is_optional():
    fields = {spec.key: spec for spec in default_fields(cfg())}
    assert fields["infos"].required and fields["infos"].multiline
    assert not fields["result_id"].required and fields["result_id"].max_length == 60


# ------------------------------------------------------------------ buttons


def _buttons(view: discord.ui.View):
    for row in view.to_components():
        yield from row["components"]


@pytest.mark.parametrize("config", [cfg(), cfg(FORM_PAGES="3", FORM_PAGE2="P2", FORM_FIELDS_P2="a:A", FORM_PAGE3="P3")])
def test_home_card_buttons_fit_discord_limits(config):
    texts = Texts.load(config)
    for page in range(config.page_count):
        view = home_view(config, texts, page=page)
        rows = view.to_components()
        assert len(rows) <= 5, "a message can hold at most 5 action rows"
        ids = []
        for button in _buttons(view):
            assert button["type"] == 2
            assert 1 <= len(button.get("label", "")) <= 80
            assert len(button["custom_id"]) <= 100
            ids.append(button["custom_id"])
        assert f"fb:start:{page}" in ids
        if config.page_count > 1:
            assert (f"fb:prev:{page}" in ids) and (f"fb:next:{page}" in ids)
        else:
            assert all(not cid.startswith("fb:prev") for cid in ids)


def test_close_and_hide_are_secondary_or_danger():
    view = home_view(cfg(), Texts.load(cfg()))
    styles = {child["custom_id"]: child["style"] for child in _buttons(view)}
    assert styles["fb:close"] == discord.ButtonStyle.secondary.value
    admin = {child["custom_id"]: child["style"] for child in _buttons(admin_view("AB2C", Texts.load(cfg())))}
    assert admin["fb:handled:AB2C"] == discord.ButtonStyle.success.value
    assert admin["fb:hide:AB2C"] == discord.ButtonStyle.danger.value


def test_every_custom_id_dispatches_to_a_dynamic_item():
    """A custom_id no template matches = a button that does nothing, forever."""
    registered = {cls.__name__: cls for cls in dynamic_items()}
    produced = []
    for item in (
        home_view(cfg(FORM_PAGES="2", FORM_PAGE2="P2", FORM_FIELDS_P2="a:A"), Texts.load(cfg())),
        admin_view("AB2C", Texts.load(cfg())),
    ):
        produced += [child["custom_id"] for child in _buttons(item)]

    assert produced
    for custom_id in produced:
        matches = [cls for cls in registered.values() if cls.__discord_ui_compiled_template__.match(custom_id)]
        assert len(matches) == 1, f"{custom_id} matched {len(matches)} dynamic items (want exactly 1)"


def test_templates_are_anchored_and_named():
    for cls in dynamic_items():
        pattern: re.Pattern = cls.__discord_ui_compiled_template__
        assert pattern.pattern.startswith("^"), f"{cls.__name__} template must be anchored"
        assert set(pattern.groupindex) <= {"code", "page"}


# ------------------------------------------------------------------- embeds


@pytest.mark.parametrize(
    "infos",
    [
        "",
        "short",
        "line\n" * 900,  # > 4000 chars of newlines
        "`" * 40 + "```py\nprint(1)\n```",  # tries to break out of the code block
        "@everyone @here <@&1> https://discord.gg/invite " + "é" * 300,
        "a" * 4000,
    ],
)
def test_admin_embed_never_breaks_the_limits(infos):
    config = cfg()
    sub = Submission(
        code="AB2C",
        submitter_id=111111111111111111,
        submitter_tag="someone",
        result_user_id=222222222222222222,
        result_source="provided",
        infos=infos,
        note="some note " * 90,
        extra={f"field {i}": "x" * 2000 for i in range(12)},
        guild_name="A server with a very long name " * 4,
        channel_name="a-channel",
        all_admin_ids=[1, 2, 3],
    )
    embed = build_embed(config, Texts.load(config), sub, handled_by_name="admin").to_dict()
    assert len(embed["title"]) <= EMBED_LIMITS["title"]
    description = embed.get("description", "")
    assert len(description) <= EMBED_LIMITS["description"]
    assert len(embed.get("fields") or []) <= EMBED_LIMITS["fields"]
    for field in embed.get("fields") or []:
        assert 1 <= len(field["name"]) <= EMBED_LIMITS["field_name"], field["name"]
        assert 1 <= len(field["value"]) <= EMBED_LIMITS["field_value"]
        assert "```" in field["value"] or field["name"] != "Infos you got"
    assert len(embed["footer"]["text"]) <= EMBED_LIMITS["footer"]
    assert "```" not in (embed["footer"]["text"] + description)


def test_embed_stays_valid_with_a_handled_and_unverified_state():
    config = cfg()
    texts = Texts.load(config)
    for source, note in [("self", ""), ("unverified", "⚠️ cannot resolve"), ("provided", "")]:
        sub = Submission(code="ZZ99", submitter_id=1, result_user_id=2, result_source=source, infos="hello", note=note)
        embed = build_embed(config, texts, sub).to_dict()
        assert embed["title"] == "📥 New form ZZ99"
        assert any(f["name"] == "Status" for f in embed["fields"])
