"""Startup wiring: the command tree that Discord will see, and the keep-alive server."""

from __future__ import annotations

import json

import aiohttp
import discord
import pytest

from formbot.config import Config
from formbot.health import HealthState, start_health_server

MAX_NAME = 32
MAX_DESC = 100


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def build_bot(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/tmp/formbot-startup")
    monkeypatch.setenv("PERSIST_STATE", "false")
    monkeypatch.setenv("ADMIN_USER_IDS", "100000000000000001,100000000000000002")
    monkeypatch.setenv("GUILD_ID", "333333333333333333")
    from formbot.client import FormBot

    bot = FormBot(Config.load())
    for extension in ("formbot.cog_home", "formbot.cog_admin"):
        await bot.load_extension(extension)
    return bot


async def test_home_and_adminform_are_registered_with_valid_metadata(monkeypatch):
    bot = await build_bot(monkeypatch)
    try:
        home = bot.tree.get_command("home")
        assert home is not None, "/home is missing from the command tree"
        assert not home.parameters, "the card has no parameters: /home is one tap"
        assert 1 <= len(home.name) <= MAX_NAME and 1 <= len(home.description) <= MAX_DESC
        assert home.parent is None, "/home must be a root command, not a subcommand"

        group = bot.tree.get_command("adminform")
        assert group is not None, "/adminform is missing"
        assert 1 <= len(group.description) <= MAX_DESC
        children = list(group.walk_commands())
        assert {child.name for child in children} == {"add", "remove", "list", "test", "reset", "info"}
        for child in children:
            assert 1 <= len(child.description) <= MAX_DESC
            for param in child.parameters:
                assert 1 <= len(param.name) <= MAX_NAME

        add = next(child for child in children if child.name == "add")
        assert [p.name for p in add.parameters] == ["user"]
        assert add.parameters[0].required is True
        assert add.parameters[0].type is discord.AppCommandOptionType.user
        assert add.parameters[0].description, "the option needs a hint for the member"
    finally:
        await bot.close()


async def test_setup_hook_wires_state_admins_and_dynamic_items(monkeypatch):
    bot = await build_bot(monkeypatch)
    try:
        await bot.setup_hook()  # sync_commands will fail offline, everything else must not
        assert bot.store.admin_ids == (100000000000000001, 100000000000000002)
        assert bot.cfg.guild_id == 333333333333333333
        registered = bot._connection._view_store._dynamic_items.values()
        names = {cls.__name__ for cls in registered}
        assert {"StartItem", "HandledItem", "NoteItem", "HideItem"} <= names
        assert "sync failed" in (bot.sync_summary or ""), "offline sync must degrade gracefully, not raise"
    finally:
        await bot.close()


class _Resp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send_message(self, content: str = "", **kwargs) -> None:
        self.calls.append(("send_message", content))


async def test_home_command_errors_answer_in_plain_english(monkeypatch):
    from types import SimpleNamespace

    from discord import app_commands

    bot = await build_bot(monkeypatch)
    try:
        cog = bot.get_cog("Home")
        assert cog is not None

        # the /home flood-guard must say "try again in Ns", not "something went wrong"
        interaction = SimpleNamespace(response=_Resp(), client=bot, user=None)
        await cog._home_error(interaction, app_commands.CommandOnCooldown(None, 12.3))
        assert "13s" in interaction.response.calls[0][1], interaction.response.calls

        # anything else falls through to the shared tree handler
        other = SimpleNamespace(response=_Resp(), client=bot, user=None)
        await cog._home_error(other, app_commands.CheckFailure())
        assert "admin team" in other.response.calls[0][1]
    finally:
        await bot.close()


async def test_health_payload_is_nan_free_before_the_gateway_connects(monkeypatch):
    """discord.py reports latency as NaN until the first heartbeat; NaN is not JSON."""
    from formbot.client import FormBot

    monkeypatch.setenv("PERSIST_STATE", "false")
    monkeypatch.setenv("DATA_DIR", "/tmp/formbot-startup")
    bot = FormBot(Config.load())
    try:
        payload = bot.health()
        assert payload["ready"] is False and payload["gateway_latency_ms"] is None
        assert "NaN" not in json.dumps(payload)
        # discord.py derives latency from the websocket: fake a connected shard.
        class FakeWS:  # the minimum the client needs to look connected
            latency = 0.1234
            open = True

            async def close(self, *, code: int = 1000) -> None:
                return None

        bot.ws = FakeWS()
        assert bot.latency == 0.1234
        assert bot.health()["gateway_latency_ms"] == 123.4
    finally:
        await bot.close()


async def test_health_server_answers_pings():
    state = HealthState()
    runner = await start_health_server(state, port=0, host="127.0.0.1")
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/health") as response:
                assert response.status == 200
                body = await response.text()
                # "NaN"/"Infinity" would be invalid JSON and break strict monitors.
                assert "NaN" not in body and "Infinity" not in body, body
                payload = json.loads(body, parse_constant=lambda bad: (_ for _ in ()).throw(ValueError(bad)))
                assert payload["status"] == "starting" and payload["ready"] is False
                assert payload.get("gateway_latency_ms") is None

            async with session.get(f"{base}/ready") as response:
                assert response.status == 503, "not connected yet -> Render may restart the container"

            async with session.get(f"{base}/ping") as response:
                assert response.status == 200 and await response.text() == "pong"

            async with session.get(f"{base}/") as response:
                assert response.status == 200
                body = await response.text()
                assert "discord-home-form-bot" in body and "/health" in body

            async with session.post(f"{base}/admin/ping") as response:
                assert response.status == 404, "no PING_TOKEN configured => endpoint stays closed"
            async with session.get(f"{base}/nope") as response:
                assert response.status == 404
            assert state.pings >= 4
    finally:
        await runner.cleanup()


async def test_health_server_reports_the_bot_and_honours_ping_token():
    class DummyStore:
        def ping(self):
            return 1

        def prune_cooldowns(self):
            return 0

    class DummyBot:
        is_ready = lambda self: True  # noqa: E731
        latency = float("nan")  # what discord.py reports before the first heartbeat
        store = DummyStore()
        cfg = type("C", (), {"ping_token": "s3cret"})()

        def health(self):
            return {"status": "ok", "ready": True, "user": "FormBot#1234", "admins": 2, "servers": 1}

    state = HealthState(bot=DummyBot())
    runner = await start_health_server(state, port=0, host="127.0.0.1")
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/health") as response:
                body = await response.text()
                assert "NaN" not in body and "Infinity" not in body, body
                payload = json.loads(body, parse_constant=lambda bad: (_ for _ in ()).throw(ValueError(bad)))
                assert payload["ready"] is True and payload["admins"] == 2
                assert payload["user"] == "FormBot#1234"
            async with session.get(f"{base}/ready") as response:
                assert response.status == 200
            async with session.post(f"{base}/admin/ping") as response:
                assert response.status == 401
            async with session.post(f"{base}/admin/ping", headers={"Authorization": "Bearer s3cret"}) as response:
                assert response.status == 200 and "s3cret" not in await response.text()
    finally:
        await runner.cleanup()
