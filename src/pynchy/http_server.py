"""Embedded HTTP server for health checks, remote deploys, and TUI API.

Exposes endpoints on 0.0.0.0:DEPLOY_PORT. Access is controlled by
Tailscale ACLs and the machine firewall.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from aiohttp import web

from pynchy.config import ASSISTANT_NAME, DEPLOY_PORT, PROJECT_ROOT
from pynchy.deploy import finalize_deploy
from pynchy.logger import logger
from pynchy.types import NewMessage

_start_time = time.monotonic()


def _get_head_sha() -> str:
    """Return the current git HEAD SHA, or 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


class HttpDeps(Protocol):
    """Dependencies injected by app.py."""

    async def send_message(self, jid: str, text: str) -> None: ...

    def main_chat_jid(self) -> str: ...

    def channels_connected(self) -> bool: ...

    # --- TUI API deps ---

    def get_groups(self) -> list[dict[str, Any]]: ...

    async def get_messages(self, jid: str, limit: int) -> list[NewMessage]: ...

    async def send_user_message(self, jid: str, content: str) -> None: ...

    def subscribe_events(
        self, callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
    ) -> Callable[[], None]: ...


# ------------------------------------------------------------------
# Existing endpoints
# ------------------------------------------------------------------


async def _handle_health(request: web.Request) -> web.Response:
    deps: HttpDeps = request.app["deps"]
    return web.json_response(
        {
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - _start_time),
            "head_sha": _get_head_sha(),
            "channels_connected": deps.channels_connected(),
        }
    )


async def _handle_deploy(request: web.Request) -> web.Response:
    deps: HttpDeps = request.app["deps"]
    old_sha = _get_head_sha()

    # 1. git pull --ff-only
    pull = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if pull.returncode != 0:
        msg = f"git pull --ff-only failed: {pull.stderr.strip()}"
        logger.error("Deploy failed", error=msg)
        chat_jid = deps.main_chat_jid()
        if chat_jid:
            await deps.send_message(
                chat_jid,
                f"{ASSISTANT_NAME}: Deploy failed — {msg}",
            )
        return web.json_response({"error": msg}, status=409)

    new_sha = _get_head_sha()

    # 2. Validate import
    validate = subprocess.run(
        ["python", "-c", "import pynchy"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if validate.returncode != 0:
        err = validate.stderr.strip()[-300:]
        logger.error("Deploy validation failed, rolling back", error=err)
        subprocess.run(
            ["git", "reset", "--hard", old_sha],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
        )
        chat_jid = deps.main_chat_jid()
        if chat_jid:
            msg = f"Deploy failed — import validation error, rolled back to {old_sha[:8]}."
            await deps.send_message(chat_jid, f"{ASSISTANT_NAME}: {msg}")
        return web.json_response(
            {"error": "import validation failed", "rolled_back_to": old_sha},
            status=422,
        )

    # 3. Write continuation, notify WhatsApp, and schedule SIGTERM
    chat_jid = deps.main_chat_jid()
    await finalize_deploy(
        send_message=deps.send_message,
        chat_jid=chat_jid,
        commit_sha=new_sha,
        previous_sha=old_sha,
        sigterm_delay=0.5,  # let the HTTP response flush first
    )

    return web.json_response({"status": "restarting", "sha": new_sha, "previous_sha": old_sha})


# ------------------------------------------------------------------
# TUI API endpoints
# ------------------------------------------------------------------


async def _handle_api_groups(request: web.Request) -> web.Response:
    """Return registered groups."""
    deps: HttpDeps = request.app["deps"]
    return web.json_response(deps.get_groups())


async def _handle_api_messages(request: web.Request) -> web.Response:
    """Return chat history for a group."""
    deps: HttpDeps = request.app["deps"]
    jid = request.query.get("jid", "")
    if not jid:
        return web.json_response({"error": "jid parameter required"}, status=400)
    limit = int(request.query.get("limit", "50"))
    messages = await deps.get_messages(jid, limit)
    return web.json_response(
        [
            {
                "sender_name": m.sender_name,
                "content": m.content,
                "timestamp": m.timestamp,
                "is_from_me": m.is_from_me,
            }
            for m in messages
        ]
    )


async def _handle_api_send(request: web.Request) -> web.Response:
    """Send a message from the TUI client."""
    deps: HttpDeps = request.app["deps"]
    body = await request.json()
    jid = body.get("jid", "")
    content = body.get("content", "")
    if not jid or not content:
        return web.json_response({"error": "jid and content required"}, status=400)
    await deps.send_user_message(jid, content)
    return web.json_response({"status": "ok"})


async def _handle_api_events(request: web.Request) -> web.StreamResponse:
    """SSE stream for real-time events (messages, agent activity)."""
    deps: HttpDeps = request.app["deps"]

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_event(event: dict[str, Any]) -> None:
        await queue.put(event)

    unsubscribe = deps.subscribe_events(on_event)

    try:
        while True:
            event = await queue.get()
            data = json.dumps(event)
            await response.write(f"data: {data}\n\n".encode())
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        unsubscribe()

    return response


# ------------------------------------------------------------------
# Server setup
# ------------------------------------------------------------------


async def start_http_server(deps: HttpDeps) -> web.AppRunner:
    """Create, start, and return the HTTP server runner."""
    app = web.Application()
    app["deps"] = deps
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/deploy", _handle_deploy)
    app.router.add_get("/api/groups", _handle_api_groups)
    app.router.add_get("/api/messages", _handle_api_messages)
    app.router.add_post("/api/send", _handle_api_send)
    app.router.add_get("/api/events", _handle_api_events)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DEPLOY_PORT)
    await site.start()
    logger.info("HTTP server listening", port=DEPLOY_PORT)
    return runner
