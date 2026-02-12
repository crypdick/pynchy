"""Embedded HTTP server for health checks and remote deploys.

Exposes /health (GET) and /deploy (POST) endpoints, bound to 0.0.0.0
on DEPLOY_PORT. Access is controlled by Tailscale ACLs and the machine firewall.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from typing import Protocol

from aiohttp import web

from pynchy.config import ASSISTANT_NAME, DATA_DIR, DEPLOY_PORT, PROJECT_ROOT
from pynchy.logger import logger

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

    # 3. Write deploy continuation
    chat_jid = deps.main_chat_jid()
    continuation = {
        "chat_jid": chat_jid,
        "session_id": "",
        "resume_prompt": "Deploy complete. Verifying service health.",
        "commit_sha": new_sha,
        "previous_commit_sha": old_sha,
    }
    continuation_path = DATA_DIR / "deploy_continuation.json"
    continuation_path.parent.mkdir(parents=True, exist_ok=True)
    continuation_path.write_text(json.dumps(continuation, indent=2))

    # 4. Respond before restarting
    body = {
        "status": "restarting",
        "sha": new_sha,
        "previous_sha": old_sha,
    }

    # 5. Notify WhatsApp
    if chat_jid:
        await deps.send_message(
            chat_jid,
            f"{ASSISTANT_NAME}: Deploying {new_sha[:8]}... restarting now.",
        )

    logger.info("Deploy: restarting service", old_sha=old_sha, new_sha=new_sha)

    # 6. Schedule SIGTERM after a short delay so the response is sent first
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

    return web.json_response(body)


async def start_http_server(deps: HttpDeps) -> web.AppRunner:
    """Create, start, and return the HTTP server runner."""
    app = web.Application()
    app["deps"] = deps
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/deploy", _handle_deploy)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DEPLOY_PORT)
    await site.start()
    logger.info("HTTP server listening", port=DEPLOY_PORT)
    return runner
