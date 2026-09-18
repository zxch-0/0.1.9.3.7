"""End-to-end flow on fake Discord objects: /home card → modal → admin DMs → lock → note.

No network and no token needed: it drives the real functions with stubs, which is what
catches the "worked on my machine" bugs (typo'd attribute, missing await, state that is
never recorded).
"""

from __future__ import annotations

import itertools

import discord

from formbot.cog_admin import _can_manage
from formbot.config import Config
from formbot.store import StateStore
from formbot.texts import Texts
from formbot.views import (
    FormModal,
    HandledItem,
    HideItem,
    NextItem,
    NoteModal,
    PrevItem,
    StartItem,
    home_view,
)

NEXT_ID = itertools.count(9_000_000_000_000_000_001)


class FakeResp:
    """discord.py's HTTPException reads ``response.status`` when it is built."""

    def __init__(self, status: int = 400) -> None:
        self.status = status
        self.reason = "error"


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(FakeResp(403), {"code": 50007, "message": "Cannot send messages to this user"})


def not_found(message: str = "Unknown User", code: int = 10013) -> discord.NotFound:
    return discord.NotFound(FakeResp(404), {"code": code, "message": message})

MEMBER = 555111111111111111
FRIEND = 777222222222222222
ADMIN_A = 100000000000000001
ADMIN_B = 100000000000000002
STRANGER = 666000000000000001


class FakeMessage:
    def __init__(self, channel: FakeDM, **kwargs) -> None:
        self.id = next(NEXT_ID)
        self.channel = channel
        self.embeds = list(kwargs.get("embeds") or ([kwargs["embed"]] if kwargs.get("embed") else []))
        self.view = kwargs.get("view")
        self.edits: list[dict] = []
        self.deleted = False

    async def edit(self, **kwargs) -> FakeMessage:
        self.edits.append(kwargs)
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]
        if "view" in kwargs:
            self.view = kwargs["view"]
        return self

    async def delete(self) -> None:
        self.deleted = True


class FakeDM:
    def __init__(self, user: FakeUser) -> None:
        self.user = user
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[dict] = []

    async def send(self, **kwargs) -> FakeMessage:
        if self.user.blocks:
            raise forbidden()
        if self.user.booms:
            raise discord.HTTPException(None, "discord said no")
        message = FakeMessage(self, **kwargs)
        self.messages[message.id] = message
        self.sent.append(kwargs)
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        return self.messages[message_id]


class FakeUser:
    def __init__(self, user_id: int, name: str = "someone", *, blocks: bool = False, booms: bool = False) -> None:
        self.id = user_id
        self.display_name = name
        self.blocks = blocks
        self.booms = booms
        self.display_avatar = type("Avatar", (), {"url": f"https://cdn.example/{user_id}.png"})()
        self.dm = FakeDM(self)

    async def create_dm(self) -> FakeDM:
        return self.dm

    async def send(self, **kwargs) -> FakeMessage:
        return await self.dm.send(**kwargs)


class FakePerms:
    def __init__(self, admin: bool = False) -> None:
        self.administrator = admin


class FakeAdminUser(FakeUser):
    """A member that has the Administrator permission (that is what the gate checks)."""

    def __init__(self, user_id: int, name: str = "admin-member", *, admin: bool = True, **kwargs) -> None:
        super().__init__(user_id, name, **kwargs)
        self.guild_permissions = FakePerms(admin)


class FakeGuild:
    def __init__(self, name: str = "Test Server", members=(), owner: int = 0) -> None:
        self.id = next(NEXT_ID)
        self.name = name
        self.owner_id = owner
        self.me = FakeAdminUser(999000000000000000, "FormBot")
        self._members = {m.id: m for m in members}

    def get_member(self, member_id: int):
        return self._members.get(member_id)

    async def fetch_member(self, member_id: int):
        if member_id in self._members:
            return self._members[member_id]
        raise not_found("Unknown Member", 10007)


class FakeChannel:
    def __init__(self, name: str = "general") -> None:
        self.id = next(NEXT_ID)
        self.name = name


class _Calls:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _record(self, kind: str, **kwargs) -> None:
        self.calls.append((kind, kwargs))

    @property
    def first(self) -> dict:
        return self.calls[0][1]

    @property
    def last(self) -> dict:
        return self.calls[-1][1]

    def text(self) -> str:
        parts = []
        for _, kwargs in self.calls:
            for key in ("content",):
                if kwargs.get(key):
                    parts.append(str(kwargs[key]))
            embed = kwargs.get("embed")
            if embed is not None:
                parts.append(f"{embed.title or ''} {embed.description or ''}")
        return " | ".join(parts)

    def is_done(self) -> bool:
        return bool(self.calls)


class FakeResponse(_Calls):
    async def send_message(self, content=None, **kwargs) -> None:
        self._record("send_message", content=content, **kwargs)

    async def defer(self, **kwargs) -> None:
        self._record("defer", **kwargs)

    async def edit_message(self, **kwargs) -> None:
        self._record("edit_message", **kwargs)

    async def send_modal(self, modal) -> None:
        self._record("send_modal", modal=modal)


class FakeFollowup(_Calls):
    async def send(self, content=None, **kwargs) -> None:
        self._record("send", content=content, **kwargs)


class FakeInteraction:
    def __init__(self, client, user, *, guild=None, channel=None) -> None:
        self._client = client
        self.user = user
        self.guild = guild
        self.channel = channel or FakeChannel()
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message: FakeMessage | None = None

    @property
    def client(self):
        return self._client

    async def delete_original_response(self) -> None:
        self.response._record("delete")


class FakeBot:
    def __init__(self, cfg: Config, users: dict[int, FakeUser]) -> None:
        self.cfg = cfg
        self.texts = Texts.load(cfg)
        self.user = FakeUser(999000000000000000, "FormBot")
        self._users = dict(users)
        self.latency = 0.042
        self.store: StateStore = StateStore(None, admins=(), persist=False)

    def get_user(self, user_id: int):
        return self._users.get(user_id)

    async def fetch_user(self, user_id: int):
        if user_id in self._users:
            return self._users[user_id]
        raise not_found()

    def is_ready(self) -> bool:
        return True

    def health(self) -> dict:
        return {"ready": True}


def make_bot(monkeypatch, *, admins=(ADMIN_A, ADMIN_B), cooldown="0", friend=True, blocked_admins=()):
    monkeypatch.setenv("DATA_DIR", "/tmp/formbot-test")
    monkeypatch.setenv("PERSIST_STATE", "false")
    monkeypatch.setenv("FORM_COOLDOWN_SECONDS", cooldown)
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    cfg = Config.load()

    users = {
        MEMBER: FakeUser(MEMBER, "member"),
        ADMIN_A: FakeUser(ADMIN_A, "adminA", blocks=ADMIN_A in blocked_admins),
        ADMIN_B: FakeUser(ADMIN_B, "adminB", blocks=ADMIN_B in blocked_admins),
        STRANGER: FakeUser(STRANGER, "intruder"),
    }
    if friend:
        users[FRIEND] = FakeUser(FRIEND, "friend")
    if not admins:
        users.pop(ADMIN_A)
        users.pop(ADMIN_B)

    bot = FakeBot(cfg, users)
    bot.store = StateStore(None, admins=admins, persist=False)
    bot.guild = FakeGuild(members=[u for k, u in users.items() if k in (MEMBER, FRIEND)])
    return bot


def fill(modal: FormModal, **values: str) -> FormModal:
    for key, value in values.items():
        modal.fields[key]._value = value
    return modal


async def submit(bot, *, infos="I need 3 accounts, proof attached", result_id="", member=None, guild="keep"):
    member = member or bot.get_user(MEMBER)
    interaction = FakeInteraction(bot, member, guild=bot.guild if guild == "keep" else None)
    modal = fill(FormModal(cfg=bot.cfg, texts=bot.texts), infos=infos, result_id=result_id)
    await modal.on_submit(interaction)
    return interaction


# ------------------------------------------------------------------------ card


async def test_start_button_opens_the_modal(monkeypatch):
    bot = make_bot(monkeypatch)
    interaction = FakeInteraction(bot, bot.get_user(MEMBER), guild=bot.guild)
    await StartItem(page=0, label="START", emoji="▶️").callback(interaction)

    kind, kwargs = interaction.response.calls[0]
    assert kind == "send_modal"
    assert list(kwargs["modal"].fields) == ["infos", "result_id"]


async def test_start_button_respects_the_cooldown(monkeypatch):
    bot = make_bot(monkeypatch, cooldown="60")
    bot.store.touch(MEMBER)
    interaction = FakeInteraction(bot, bot.get_user(MEMBER), guild=bot.guild)
    await StartItem(page=0, label="START", emoji=None).callback(interaction)

    kind, kwargs = interaction.response.calls[0]
    assert kind == "send_message" and "60s" in kwargs["content"]
    assert interaction.response.calls == [(kind, kwargs)]  # no modal opened


async def test_card_layout_has_start_on_its_own_row(monkeypatch):
    cfg = Config.load()
    view = home_view(cfg, Texts.load(cfg))
    rows = view.to_components()
    assert [len(row["components"]) for row in rows] == [1, 1]
    assert rows[0]["components"][0]["style"] == discord.ButtonStyle.success.value
    assert rows[0]["components"][0]["label"] == cfg.start_label


async def test_next_and_back_move_between_pages(monkeypatch):
    monkeypatch.setenv("FORM_PAGES", "3")
    monkeypatch.setenv("FORM_PAGE2", "Proof")
    monkeypatch.setenv("FORM_FIELDS_P2", "proof:Link")
    monkeypatch.setenv("FORM_PAGE3", "Notes")
    monkeypatch.setenv("FORM_FIELDS_P3", "notes:Notes:long")
    bot = make_bot(monkeypatch, admins=(ADMIN_A,))

    interaction = FakeInteraction(bot, bot.get_user(MEMBER), guild=bot.guild)
    await NextItem(page=0, label="Next").callback(interaction)

    edited = interaction.response.calls[0][1]
    assert edited["embed"].title == "Proof"
    assert "Page 2 of 3" in edited["embed"].author.name
    ids = [child["custom_id"] for row in edited["view"].to_components() for child in row["components"]]
    assert "fb:next:1" in str(ids) and "fb:prev:1" in str(ids)

    back = FakeInteraction(bot, bot.get_user(MEMBER), guild=bot.guild)
    await PrevItem(page=1, label="Back").callback(back)
    assert back.response.calls[0][1]["embed"].title == bot.cfg.title


async def test_nav_clamps_at_the_edges(monkeypatch):
    monkeypatch.setenv("FORM_PAGES", "2")
    monkeypatch.setenv("FORM_PAGE2", "Second")
    monkeypatch.setenv("FORM_FIELDS_P2", "a:A")
    bot = make_bot(monkeypatch, admins=(ADMIN_A,))
    interaction = FakeInteraction(bot, bot.get_user(MEMBER), guild=bot.guild)
    await PrevItem(page=0, label="Back").callback(interaction)
    assert interaction.response.calls[0][1]["embed"].title == bot.cfg.title  # cannot go before page 1


async def test_admin_gate_allows_owner_and_admin_only(monkeypatch):
    bot = make_bot(monkeypatch, admins=(ADMIN_A,))

    async def allowed(user, guild):
        return _can_manage(FakeInteraction(bot, user, guild=guild))

    assert await allowed(bot.get_user(ADMIN_A), bot.guild) is True  # on the team
    assert await allowed(FakeUser(123456789012345678, "owner"), FakeGuild(owner=123456789012345678)) is True
    assert await allowed(FakeAdminUser(1, "mod"), bot.guild) is True  # administrator member
    assert await allowed(bot.get_user(STRANGER), bot.guild) is False
    assert await allowed(bot.get_user(STRANGER), None) is False  # nobody in a DM
    assert await allowed(bot.get_user(ADMIN_A), None) is True  # team member, even in a DM


# ---------------------------------------------------------------------- submit


async def test_submit_reaches_every_admin_and_confirms_privately(monkeypatch):
    bot = make_bot(monkeypatch)
    interaction = await submit(bot, infos="  I need 3 accounts, proof attached  ")

    assert interaction.response.calls[0] == ("defer", {"ephemeral": True, "thinking": True})
    sub = bot.store.submissions[0]
    assert sub.infos == "I need 3 accounts, proof attached"
    assert sub.result_user_id == MEMBER and sub.result_source == "self"
    assert sub.guild_name == "Test Server" and sub.submitter_id == MEMBER

    for admin_id in (ADMIN_A, ADMIN_B):
        admin = bot.get_user(admin_id)
        sent = admin.dm.sent[0]
        assert sent["embed"].title == f"📥 New form {sub.code}"
        assert isinstance(sent["view"], discord.ui.View)
        assert sub.admin_message_ids[str(admin_id)] in admin.dm.messages  # needed to lock it later

    confirm = interaction.followup.calls[0][1]
    assert sub.code in confirm["content"] and confirm["ephemeral"] is True


async def test_second_submit_is_blocked_by_the_cooldown(monkeypatch):
    bot = make_bot(monkeypatch, cooldown="45")
    await submit(bot, infos="first one")
    assert len(bot.store.submissions) == 1

    blocked = await submit(bot, infos="second one")
    assert len(bot.store.submissions) == 1
    assert "45s" in blocked.response.calls[-1][1]["content"]


def _id_case(raw, expected_source, expected_target):
    async def run(monkeypatch):
        bot = make_bot(monkeypatch, admins=(ADMIN_A,))
        await submit(bot, infos="please help", result_id=raw)
        sub = bot.store.submissions[0]
        assert sub.result_source == expected_source
        assert sub.result_user_id == expected_target
        if raw and expected_target != FRIEND:
            assert sub.note  # the admin must be told why the typed value was ignored
    return run


test_id_left_empty_goes_back_to_submitter = _id_case("", "self", MEMBER)
test_id_pointing_at_a_server_member_is_verified = _id_case(str(FRIEND), "provided", FRIEND)
test_mention_syntax_is_understood = _id_case(f"<@!{FRIEND}>", "provided", FRIEND)
test_phone_number_is_not_an_id = _id_case("0612345678", "self", MEMBER)
test_username_is_not_an_id = _id_case("someuser", "self", MEMBER)
test_id_longer_than_a_snowflake_is_not_an_id = _id_case("1" * 25, "self", MEMBER)


async def test_foreign_id_is_flagged_but_results_stay_reachable(monkeypatch):
    bot = make_bot(monkeypatch, admins=(ADMIN_A,), friend=False)
    await submit(bot, infos="hi there", result_id="888000000000000000")
    sub = bot.store.submissions[0]
    assert sub.result_user_id == MEMBER  # never chase an ID we cannot verify
    assert "888 000 000 000 000 000" in sub.note


async def test_too_short_and_empty_answers_are_refused(monkeypatch):
    bot = make_bot(monkeypatch, admins=(ADMIN_A,))
    empty = await submit(bot, infos="   ")
    assert "first box" in empty.response.calls[0][1]["content"]

    short = await submit(bot, infos="ab")
    assert "at least 3" in short.response.calls[0][1]["content"]
    assert not bot.store.submissions


async def test_long_answers_become_an_attachment(monkeypatch):
    bot = make_bot(monkeypatch, admins=(ADMIN_A,))
    await submit(bot, infos="x" * 1500)
    sub = bot.store.submissions[0]
    sent = bot.get_user(ADMIN_A).dm.sent[0]
    assert sent["file"].filename == f"form-{sub.code}.txt"
    assert any("full text attached" in field.value for field in sent["embed"].fields)


async def test_blocked_admins_are_reported_to_the_member(monkeypatch):
    bot = make_bot(monkeypatch, admins=(ADMIN_A,), blocked_admins=(ADMIN_A,))
    interaction = await submit(bot)
    assert "no admin could be reached" in interaction.followup.calls[0][1]["content"]
    assert bot.store.submissions[0].delivery[str(ADMIN_A)] == "forbidden"


async def test_form_is_kept_when_nobody_is_configured(monkeypatch):
    bot = make_bot(monkeypatch, admins=())
    interaction = await submit(bot)
    assert "nobody is configured" in interaction.followup.calls[0][1]["content"]
    assert len(bot.store.submissions) == 1  # the answer itself is never lost


# ------------------------------------------------------------------ admin lock


async def test_first_admin_locks_the_form_for_everyone(monkeypatch):
    bot = make_bot(monkeypatch)
    submitted = await submit(bot)
    sub = bot.store.submissions[0]
    assert "Sent. An admin received your form" in submitted.followup.calls[0][1]["content"]

    admin_a = bot.get_user(ADMIN_A)
    claim = FakeInteraction(bot, admin_a, channel=admin_a.dm)
    await HandledItem(code=sub.code, label="✅ Mark as handled").callback(claim)

    assert sub.is_handled and sub.handled_by == ADMIN_A
    edited = claim.response.calls[-1][1]
    assert edited["view"] is None and "Handled by adminA" in edited["embed"].footer.text

    other = bot.get_user(ADMIN_B)
    other_message = other.dm.messages[sub.admin_message_ids[str(ADMIN_B)]]
    assert other_message.view is None and "Handled by adminA" in other_message.embeds[0].footer.text

    member = bot.get_user(MEMBER)
    assert "marked it as handled" in member.dm.sent[-1]["embed"].description


async def test_second_admin_is_told_it_is_taken(monkeypatch):
    bot = make_bot(monkeypatch)
    await submit(bot)
    sub = bot.store.submissions[0]

    admin_a, admin_b = bot.get_user(ADMIN_A), bot.get_user(ADMIN_B)
    await HandledItem(code=sub.code, label="handled").callback(FakeInteraction(bot, admin_a, channel=admin_a.dm))
    second = FakeInteraction(bot, admin_b, channel=admin_b.dm)
    await HandledItem(code=sub.code, label="handled").callback(second)

    assert sub.handled_by == ADMIN_A  # unchanged
    assert second.response.calls[-1][1]["view"] is None  # their copy is disarmed too


async def test_strangers_cannot_use_admin_buttons(monkeypatch):
    bot = make_bot(monkeypatch)
    await submit(bot)
    sub = bot.store.submissions[0]

    intruder = bot.get_user(STRANGER)
    interaction = FakeInteraction(bot, intruder, channel=intruder.dm)
    await HandledItem(code=sub.code, label="handled").callback(interaction)

    assert not sub.is_handled and sub.handled_by is None
    assert "admin team" in interaction.response.calls[0][1]["content"]


async def test_unknown_code_after_a_redeploy_is_handled_politely(monkeypatch):
    bot = make_bot(monkeypatch)
    admin = bot.get_user(ADMIN_A)
    interaction = FakeInteraction(bot, admin, channel=admin.dm)
    await HandledItem(code="ZZZZ", label="handled").callback(interaction)
    assert interaction.response.calls == [("send_message", interaction.response.calls[0][1])]
    assert "ZZZZ" in interaction.response.calls[0][1]["content"]
    assert interaction.response.calls[0][1]["ephemeral"] is True


async def test_hide_removes_only_that_admins_copy(monkeypatch):
    bot = make_bot(monkeypatch)
    await submit(bot)
    sub = bot.store.submissions[0]

    admin_a, admin_b = bot.get_user(ADMIN_A), bot.get_user(ADMIN_B)
    own = admin_a.dm.messages[sub.admin_message_ids[str(ADMIN_A)]]
    interaction = FakeInteraction(bot, admin_a, channel=admin_a.dm)
    interaction.message = own
    await HideItem(code=sub.code, label="🗑️ Hide").callback(interaction)

    assert own.deleted and sub.delivery[str(ADMIN_A)] == "hidden"
    assert not admin_b.dm.messages[sub.admin_message_ids[str(ADMIN_B)]].deleted


# ------------------------------------------------------------------------ note


async def test_note_reaches_the_member_and_is_logged(monkeypatch):
    bot = make_bot(monkeypatch, admins=(ADMIN_A,))
    await submit(bot)
    sub = bot.store.submissions[0]

    admin = bot.get_user(ADMIN_A)
    modal = NoteModal(code=sub.code, texts=bot.texts, target_name="member")
    modal.note._value = "results are ready, check your email"
    interaction = FakeInteraction(bot, admin, channel=admin.dm)
    await modal.on_submit(interaction)

    note_embed = bot.get_user(MEMBER).dm.sent[-1]["embed"]
    assert sub.code in note_embed.title and "results are ready" in note_embed.description
    assert sub.notes[0]["delivered"] and sub.notes[0]["admin_id"] == ADMIN_A
    assert "Sent to" in interaction.followup.calls[0][1]["content"]


async def test_note_failure_is_told_to_the_admin(monkeypatch):
    bot = make_bot(monkeypatch, admins=(ADMIN_A,))
    await submit(bot)
    sub = bot.store.submissions[0]
    bot.get_user(MEMBER).blocks = True

    admin = bot.get_user(ADMIN_A)
    modal = NoteModal(code=sub.code, texts=bot.texts, target_name="member")
    modal.note._value = "here are your results"
    interaction = FakeInteraction(bot, admin, channel=admin.dm)
    await modal.on_submit(interaction)

    assert "Could not DM" in interaction.followup.calls[0][1]["content"]
    assert sub.notes[0]["delivered"] is False


async def test_note_cannot_come_from_a_non_admin(monkeypatch):
    bot = make_bot(monkeypatch, admins=(ADMIN_A,))
    await submit(bot)
    sub = bot.store.submissions[0]

    stranger = bot.get_user(STRANGER)
    modal = NoteModal(code=sub.code, texts=bot.texts, target_name="member")
    modal.note._value = "pretending to be staff"
    interaction = FakeInteraction(bot, stranger, channel=stranger.dm)
    await modal.on_submit(interaction)

    assert "admin team" in interaction.response.calls[0][1]["content"]
    assert not sub.notes
