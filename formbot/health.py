"""A ~40-line HTTP server whose only job is to keep Render's free tier awake.

Render's free web services spin down after 15 minutes *without inbound traffic*.
An external monitor (UptimeRobot, a GitHub Action cron, cron-job.org...) hitting
``/health`` every 5 minutes keeps the process — and therefore the Discord gateway
connection — alive. Endpoints:

    GET /              a human-readable status page
    GET /health        200 + JSON, always while the HTTP server lives   <-- use this for pings
    GET /healthz       alias of /health
    GET /status        alias of /health
    GET /ready         200 only once the gateway is connected (503 before)
    GET /ping          plain text, cheapest possible answer
    POST /admin/ping   same as /ping but requires ``Authorization: Bearer $PING_TOKEN``
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

log = logging.getLogger("formbot.health")


@dataclass
class HealthState:
    """Mutable handle shared between the restart loop and the HTTP app."""

    bot: Any = None
    restarting: bool = False
    started_at: float = field(default_factory=time.monotonic)
    pings: int = 0
    last_ping: float | None = None

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)


try:  # aiohttp >= 3.9 prefers typed AppKey objects over bare strings
    STATE_KEY: Any = web.AppKey("state", HealthState)
except AttributeError:  # pragma: no cover - older aiohttp
    STATE_KEY = "state"


def _payload(state: HealthState) -> dict[str, Any]:
    data: dict[str, Any] = {
        "service": "discord-home-form-bot",
        "http_uptime_seconds": state.uptime_seconds,
        "pings": state.pings,
        "restarting": state.restarting,
    }
    if state.bot is not None:
        try:
            data.update(state.bot.health())
        except Exception as exc:  # never let a status bug kill the keep-alive
            data["bot_health_error"] = str(exc)
    else:
        data["status"] = "starting"
        data["ready"] = False
    return data


async def _bump(state: HealthState) -> None:
    state.pings += 1
    state.last_ping = time.time()
    bot = state.bot
    if bot is not None:
        try:
            bot.store.ping()
            bot.store.prune_cooldowns()
        except Exception:
            pass


async def handle_health(request: web.Request) -> web.Response:
    state: HealthState = request.app[STATE_KEY]
    await _bump(state)
    return web.json_response(_payload(state), headers={"Cache-Control": "no-store"})


async def handle_ready(request: web.Request) -> web.Response:
    state: HealthState = request.app[STATE_KEY]
    await _bump(state)
    ready = bool(state.bot is not None and state.bot.is_ready())
    return web.json_response({"ready": ready, **_payload(state)}, status=200 if ready else 503)


async def handle_ping(request: web.Request) -> web.Response:
    state: HealthState = request.app[STATE_KEY]
    await _bump(state)
    return web.Response(text="pong", headers={"Cache-Control": "no-store"})


async def handle_admin_ping(request: web.Request) -> web.Response:
    state: HealthState = request.app[STATE_KEY]
    token = getattr(getattr(state.bot, "cfg", None), "ping_token", "")
    if not token:
        return web.Response(text="PING_TOKEN is not configured on the service", status=404)
    header = request.headers.get("Authorization", "")
    if header != f"Bearer {token}":
        return web.Response(text="unauthorized", status=401)
    await _bump(state)
    return web.Response(text="pong (admin ping)")


_INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{{background:#1e1f22;color:#dbdee1;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:40px}}
 .card{{max-width:620px;margin:auto;background:#2b2d31;border-radius:12px;padding:24px 28px}}
 h1{{font-size:19px;margin:0 0 6px}} code,b{{color:#00a8fc}}
 table{{width:100%;border-collapse:collapse;margin-top:14px;font-size:14px}}
 td{{padding:5px 0;border-bottom:1px solid #3f4147}} td:first-child{{color:#949ba4}}
 a{{color:#00a8fc}}
</style></head>
<body><div class="card">
<h1>🤖 {title}</h1>
<p>Keep-alive endpoint for a Discord form bot. This page is alive so the bot can stay
connected to Discord. Pinging <code>/health</code> every 5 minutes prevents Render's
15-minute idle spin-down.</p>
<table>{rows}</table>
<p>Run <code>/home</code> in Discord to open the form.</p>
</div></body></html>
"""


async def handle_index(request: web.Request) -> web.Response:
    state: HealthState = request.app[STATE_KEY]
    await _bump(state)
    payload = _payload(state)
    rows = "\n".join(
        f"<tr><td>{key}</td><td>{value}</td></tr>"
        for key, value in payload.items()
        if key not in {"service", "http_uptime_seconds"}
    )
    title = str(payload.get("title") or "discord-home-form-bot")
    return web.Response(text=_INDEX.format(title=title, rows=rows), content_type="text/html")


def create_app(state: HealthState) -> web.Application:
    app = web.Application()  # the runner below silences one log line per ping
    app[STATE_KEY] = state
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/status", handle_health)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/ready", handle_ready)
    app.router.add_post("/admin/ping", handle_admin_ping)
    return app


async def start_health_server(state: HealthState, *, port: int, host: str = "0.0.0.0") -> web.AppRunner:
    runner = web.AppRunner(create_app(state), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    log.info("health server listening on http://%s:%s/health", host, port)
    return runner
