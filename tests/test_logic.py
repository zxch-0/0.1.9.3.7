"""Pure-logic tests: env parsing, ID normalisation, store rules. No Discord, no network."""

from __future__ import annotations

import time

import pytest

from formbot.config import Config, parse_discord_id, parse_fieldset, parse_id_list
from formbot.store import CODE_ALPHABET, StateStore, Submission
from formbot.texts import Texts

# ------------------------------------------------------------------ id parsing


@pytest.mark.parametrize(
    "raw,kind,value",
    [
        ("", "empty", None),
        ("   ", "empty", None),
        (None, "empty", None),
        ("123456789012345678", "digits", 123456789012345678),
        ("<@123456789012345678>", "mention", 123456789012345678),
        ("<@!123456789012345678>", "mention", 123456789012345678),
        ("@123456789012345678", "digits", 123456789012345678),
        ("bob", "tag", None),
        ("@bob", "tag", None),
        ("bob#1234", "tag", None),
        (".bob", "tag", None),
        ("0612345678", "invalid", None),  # a phone number is not an ID
        ("1234567890123456789012345", "invalid", None),  # way too long
        ("https://discord.com/users/123456789012345678", "invalid", None),
    ],
)
def test_parse_discord_id(raw, kind, value):
    parsed = parse_discord_id(raw)
    assert parsed.kind == kind, f"{raw!r} -> {parsed.kind}"
    assert parsed.value == value


def test_tag_hint_is_used_for_friendly_message():
    assert parse_discord_id("someuser").looked_like_tag is True
    assert parse_discord_id("0612345678").looked_like_tag is False


# ------------------------------------------------------------------ env parsing


def test_parse_id_list_accepts_many_shapes():
    assert parse_id_list("1,2 3;4\n5") == (1, 2, 3, 4, 5)
    assert parse_id_list("<@123456789012345678>") == (123456789012345678,)
    assert parse_id_list("123456789012345678, 123456789012345678") == (123456789012345678,)
    assert parse_id_list("garbage, , !!") == ()
    assert parse_id_list("") == ()


def test_fieldset_parsing(monkeypatch):
    specs = parse_fieldset("proof_link:Proof link:short:true:120; notes:More notes:long:false:500:http://")
    assert [s.key for s in specs] == ["proof_link", "notes"]
    assert specs[0].label == "Proof link" and specs[0].required and specs[0].max_length == 120
    assert specs[1].multiline and specs[1].placeholder == "http://"
    assert parse_fieldset("!!bad key:whatever") == ()


def test_config_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_TOKEN", "abc.def.ghi")
    monkeypatch.setenv("ADMIN_USER_IDS", "111111111111111111, 222222222222222222")
    monkeypatch.setenv("GUILD_ID", "333333333333333333")
    monkeypatch.setenv("FORM_TITLE", "Speedrun requests")
    monkeypatch.setenv("FORM_HEADLINE", "Off-world runs only")
    monkeypatch.setenv("FORM_COOLDOWN_SECONDS", "90")
    monkeypatch.setenv("FORM_MAX_INFOS", "99999")  # clamped to the Discord cap
    monkeypatch.setenv("EMBED_COLOUR", "#ff0000")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = Config.load()
    assert cfg.token == "abc.def.ghi"
    assert cfg.admin_ids == (111111111111111111, 222222222222222222)
    assert cfg.guild_id == 333333333333333333
    assert cfg.colour_int == 0xFF0000
    assert cfg.max_infos == 4000
    assert cfg.card_pages[0][0] == "Speedrun requests"
    assert "Off-world runs only" in cfg.main_body
    assert "abc.def.ghi" not in str(cfg.safe_dict())  # token never leaks into /adminform info


def test_multi_page_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FORM_PAGES", "3")
    monkeypatch.setenv("FORM_PAGE2", "Proof")
    monkeypatch.setenv("FORM_FIELDS_P2", "proof_link:Proof link")
    monkeypatch.setenv("FORM_PAGE3", "Extra")
    monkeypatch.setenv("FORM_FIELDS_P3", "notes:Notes:long")
    cfg = Config.load()
    assert cfg.page_count == 3
    assert cfg.multi_page is True
    assert [t for t, _ in cfg.card_pages] == [cfg.title, "Proof", "Extra"]
    assert cfg.fields_for_page(0) == ()
    assert [f.key for f in cfg.fields_for_page(1)] == ["proof_link"]
    assert cfg.fields_for_page(2)[0].multiline is True
    assert cfg.label_for("notes", 2) == "Notes"
    assert cfg.label_for("unknown_key", 2) == "Unknown Key"


def test_colour_fallback(monkeypatch):
    monkeypatch.setenv("EMBED_COLOUR", "not-a-colour")
    assert Config.load().colour_int == 0x5865F2


# ----------------------------------------------------------------------- store


async def test_cooldown_blocks_then_expires():
    store = StateStore(None, admins=[1], persist=False)
    assert store.cooldown_remaining(42, 30) == 0
    store.touch(42)
    assert 25 <= store.cooldown_remaining(42, 30) <= 30
    assert store.cooldown_remaining(43, 30) == 0
    store._cooldowns[42] = time.time() - 31
    assert store.cooldown_remaining(42, 30) == 0
    assert store.cooldown_remaining(42, 0) == 0  # cooldown disabled


async def test_claim_is_a_single_winner_lock():
    store = StateStore(None, admins=[7, 8], persist=False)
    sub = await store.create(submitter_id=1, result_user_id=1, infos="hello")
    first, won_first = await store.claim(sub.code, 7)
    second, won_second = await store.claim(sub.code, 8)
    assert won_first is True and won_second is False
    assert first.is_handled and first.handled_by == 7
    assert second.handled_at is not None


async def test_codes_are_unique_and_unambiguous():
    store = StateStore(None, persist=False)
    codes = set()
    for _ in range(400):
        sub = await store.create(submitter_id=1, result_user_id=1, infos="x")
        codes.add(sub.code)
    assert len(codes) == 400
    assert all(set(code) <= set(CODE_ALPHABET) for code in codes)
    assert not any(char in code for code in codes for char in "OIL01")


async def test_store_trims_and_persists(tmp_path):
    store = StateStore(tmp_path / "state.json", admins=[5], persist=True, max_stored=20)
    for _ in range(35):
        await store.create(submitter_id=1, result_user_id=1, infos="hello")
    assert len(store.submissions) == 20
    assert store.path.is_file()

    reloaded = StateStore(tmp_path / "state.json", admins=[5], persist=True)
    await reloaded.load()
    assert len(reloaded.submissions) == 20
    assert reloaded.is_admin(5)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    tolerant = StateStore(broken, admins=[9], persist=True)
    await tolerant.load()  # must not raise
    assert tolerant.is_admin(9)


async def test_codes_beginning_with_f_are_still_findable():
    """A naive ``lstrip("#F")`` used to make those forms look lost."""

    store = StateStore(None, persist=False)
    found = 0
    for _ in range(600):
        sub = await store.create(submitter_id=1, result_user_id=1, infos="x")
        if not sub.code.startswith("F"):
            continue
        found += 1
        assert store.get(sub.code) is sub
        assert store.get(f"#{sub.code}") is sub
        assert store.get(sub.code.lower()) is sub
        await store.record_admin_message(sub.code, admin_id=1, message_id=42)
        assert sub.admin_message_ids == {"1": 42}
    assert found > 5, f"only {found} codes started with F out of 600 — generator is suspicious"

    # and every code, whatever it starts with, survives the "#"/"F-" decoration
    for _ in range(50):
        sub = await store.create(submitter_id=1, result_user_id=1, infos="y")
        for decoration in (sub.code, f"#{sub.code}", f"F-{sub.code}", f" {sub.code} "):
            assert store.get(decoration) is sub, decoration


def test_submission_roundtrip_tolerates_unknown_keys():
    data = Submission(code="ZZZZ", submitter_id=1, infos="hi", extra={"a": "b"}).to_json()
    data["from_the_future"] = 123
    back = Submission.from_json(data)
    assert back.extra == {"a": "b"} and back.code == "ZZZZ"


# -------------------------------------------------------------------- texts


def test_admin_dm_labels_follow_the_form_fields(monkeypatch):
    monkeypatch.setenv("FORM_FIELD_1_LABEL", "What do you need?")
    texts = Texts.load(Config.load())
    assert texts.admin_infos == "What do you need?"


def test_all_placeholders_are_format_safe():
    texts = Texts.load(Config.load())
    for name in vars(texts):
        value = getattr(texts, name)
        try:
            value.format(
                code="AB2C", admins=2, seconds=30, remaining=5, count=1, limit=2000, length=1,
                min=3, admin="admin", id=123, value="x", user="someone", seconds_unused=0,
            )
        except KeyError as exc:  # a placeholder nobody fills = a crash in production
            pytest.fail(f"{name} has an unknown placeholder {exc}")
        except (IndexError, ValueError) as exc:
            pytest.fail(f"{name} is not format-able: {exc}")
