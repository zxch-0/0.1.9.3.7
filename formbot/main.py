"""Process supervisor: the Discord gateway + the keep-alive HTTP server.

Why a loop instead of ``bot.run``: Render's free tier is fine as long as the process
lives, but a fatal gateway error or a stray exception must not leave a dead container
serving /health. So we recreate the bot, wait with exponential backoff, and give up
after too many consecutive failures (Render then reports a failed service instead of
silently crash-looping forever).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import signal
import sys

import discord

from .client import FormBot
from .config import Config, mask
from .health import HealthState, start_health_server

log = logging.getLogger("formbot.main")

MAX_CONSECUTIVE_FAILURES = 12
BACKOFF_BASE = 2.0
BACKOFF_CAP = 120.0


def setup_logging(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # aiohttp would otherwise log one line per keep-alive ping.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


async def _stop_watcher(state: HealthState, stop_event: asyncio.Event) -> None:
    await stop_event.wait()
    state.restarting = True
    if state.bot is not None:
        log.info("signal received — closing the gateway connection cleanly")
        with contextlib.suppress(Exception):
            await state.bot.close()


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows / non-main thread
            loop.add_signal_handler(sig, stop_event.set)


async def run_forever(cfg: Config) -> None:
    state = HealthState()
    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    runner = None
    if cfg.health_enabled:
        try:
            runner = await start_health_server(state, port=cfg.port)
        except OSError as exc:
            log.error("health server could not bind port %s: %s", cfg.port, exc)
            runner = None
    else:
        log.warning("HEALTH_ENABLED=false — Render will sleep this service after ~15 idle minutes")

    watcher = asyncio.create_task(_stop_watcher(state, stop_event))
    failures = 0
    try:
        while not stop_event.is_set():
            bot = FormBot(cfg)
            state.bot = bot
            crashed = False
            try:
                log.info("connecting to Discord…")
                await bot.start(cfg.token)
            except discord.LoginFailure as exc:
                log.critical(
                    "Discord rejected the token (%s). Fix DISCORD_TOKEN in the Render dashboard.",
                    mask(str(exc), cfg.token),
                )
                raise SystemExit(78) from exc
            except asyncio.CancelledError:
                raise
            except SystemExit:
                raise
            except Exception as exc:
                crashed = True
                log.exception("the bot went down: %s", mask(str(exc), cfg.token))
            finally:
                with contextlib.suppress(Exception):
                    await bot.close()

            if stop_event.is_set() or not crashed:
                break  # clean close (Render redeploy / SIGTERM): nothing to restart

            if bot.last_ready_at is not None:
                failures = 0  # it *did* work once, so start the backoff from scratch
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.critical("giving up after %d consecutive failures — the platform will show the error", failures)
                raise SystemExit(1)
            delay = min(BACKOFF_CAP, BACKOFF_BASE**failures) + random.uniform(0, 1)
            log.warning("restarting in %.0fs (attempt %d/%d)", delay, failures + 1, MAX_CONSECUTIVE_FAILURES)
            # wait_for(stop_event) instead of sleep(): a SIGTERM during the backoff must
            # not hold Render's ~10s shutdown window.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        log.info("shutdown complete")


def main() -> None:
    cfg = Config.load()
    setup_logging(cfg.log_level)
    if not cfg.token_present:
        log.critical(
            "DISCORD_TOKEN is not set. Locally: copy .env.example to .env. On Render: add it in Environment."
        )
        raise SystemExit(66)
    log.debug("effective config: %s", cfg.safe_dict())
    if cfg.guild_id is None:
        log.info("GUILD_ID not set — commands are registered globally and can take up to 1 hour to appear the first time")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_forever(cfg))


def debug_selftest() -> None:
    """`python main.py --selftest`: prove config, store and views import cleanly."""
    from .store import StateStore
    from .texts import Texts
    from .views import admin_view, home_embed, home_view

    cfg = Config.load()
    texts = Texts.load(cfg)
    store = StateStore(None, admins=cfg.admin_ids, persist=False)
    embed = home_embed(cfg, texts, page=0)
    view = home_view(cfg, texts, page=0)
    components = view.to_components()
    print("config      :", cfg.safe_dict())
    print("store       :", store.snapshot())
    print("embed title :", embed.title)
    print("embed desc  :", (embed.description or "")[:60].replace("\n", " "))
    print("buttons     :", [child["label"] for child in components[0]["components"]])
    print("admin view  :", [child["label"] for child in admin_view("AB2C", texts).to_components()[0]["components"]])
    print("\nOK — everything imports, the card and the admin buttons render. Set DISCORD_TOKEN to connect.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        debug_selftest()
    else:
        main()
