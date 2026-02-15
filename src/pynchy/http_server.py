"""Embedded HTTP server for health checks, remote deploys, and TUI API.

Exposes endpoints on 0.0.0.0:DEPLOY_PORT. Access is controlled by
Tailscale ACLs and the machine firewall.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from aiohttp import web

from pynchy.config import DATA_DIR, DEPLOY_PORT, PROJECT_ROOT
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


def _is_repo_dirty() -> bool:
    """Check if the working tree has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip()) if result.returncode == 0 else False
    except Exception:
        return False


def _get_head_commit_message(max_length: int = 72) -> str:
    """Return the subject line of the HEAD commit, truncated if needed."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        msg = result.stdout.strip() if result.returncode == 0 else ""
        if len(msg) > max_length:
            return msg[: max_length - 1] + "…"
        return msg
    except Exception:
        return ""


def _push_local_commits(*, skip_fetch: bool = False) -> bool:
    """Best-effort push of local commits to origin/main.

    Returns True if repo is in sync (nothing to push, or push succeeded).
    Retries once on rebase failure (covers the race where origin advances
    between fetch and rebase when two worktrees push nearly simultaneously).
    Never raises — all failures are logged and return False.
    """
    try:
        if not skip_fetch:
            fetch = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                logger.warning("push_local: git fetch failed", stderr=fetch.stderr.strip())
                return False

        count = subprocess.run(
            ["git", "rev-list", "origin/main..HEAD", "--count"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if count.returncode != 0 or int(count.stdout.strip() or "0") == 0:
            return True  # nothing to push (or can't tell)

        # Try rebase+push, retry once if origin advanced mid-operation
        for attempt in range(2):
            rebase = subprocess.run(
                ["git", "rebase", "origin/main"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            if rebase.returncode != 0:
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                )
                if attempt == 0:
                    # Re-fetch and retry — origin may have advanced
                    logger.info("push_local: rebase failed, retrying after fresh fetch")
                    retry_fetch = subprocess.run(
                        ["git", "fetch", "origin"],
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                    )
                    if retry_fetch.returncode != 0:
                        logger.warning(
                            "push_local: retry fetch failed", stderr=retry_fetch.stderr.strip()
                        )
                        return False
                    continue
                logger.warning(
                    "push_local: rebase failed after retry", stderr=rebase.stderr.strip()
                )
                return False

            push = subprocess.run(
                ["git", "push"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            if push.returncode != 0:
                logger.warning("push_local: git push failed", stderr=push.stderr.strip())
                return False

            logger.info("push_local: pushed local commits")
            return True

        return False  # exhausted attempts
    except Exception as exc:
        logger.warning("push_local: unexpected error", err=str(exc))
        return False


def _write_boot_warning(message: str) -> None:
    """Append a warning to boot_warnings.json, picked up by _send_boot_notification on restart."""
    path = DATA_DIR / "boot_warnings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        warnings = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        warnings = []
    warnings.append(message)
    path.write_text(json.dumps(warnings))


class HttpDeps(Protocol):
    """Dependencies injected by app.py."""

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    def god_chat_jid(self) -> str: ...

    def channels_connected(self) -> bool: ...

    # --- TUI API deps ---

    def get_groups(self) -> list[dict[str, Any]]: ...

    async def get_messages(self, jid: str, limit: int) -> list[NewMessage]: ...

    async def send_user_message(self, jid: str, content: str) -> None: ...

    def subscribe_events(
        self, callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
    ) -> Callable[[], None]: ...

    async def get_periodic_agents(self) -> list[dict[str, Any]]: ...


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
            "head_commit": _get_head_commit_message(),
            "dirty": _is_repo_dirty(),
            "channels_connected": deps.channels_connected(),
        }
    )


async def _handle_deploy(request: web.Request) -> web.Response:
    deps: HttpDeps = request.app["deps"]
    old_sha = _get_head_sha()

    # 1. Push any local commits before pulling (prevents divergence)
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if not _push_local_commits(skip_fetch=True):
        logger.warning("Pre-deploy push failed, continuing with rebase")

    # 2. Stash dirty files so they don't block the rebase
    stash = subprocess.run(
        ["git", "stash"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    stashed = stash.returncode == 0 and "No local changes" not in stash.stdout

    # 3. Rebase to incorporate incoming remote changes
    pull = subprocess.run(
        ["git", "rebase", "origin/main"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if pull.returncode != 0:
        # Abort failed rebase to leave repo clean, then continue with current code
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
        )
        logger.warning(
            "git rebase failed, restarting with current code", stderr=pull.stderr.strip()
        )
        _write_boot_warning(
            "Deploy rolled back to previous commit because incoming commits failed to rebase. "
            "Please reconcile the incoming changes into your local clone, push, then redeploy."
        )

    # Restore stashed files regardless of rebase outcome
    if stashed:
        subprocess.run(
            ["git", "stash", "pop"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )

    new_sha = _get_head_sha()
    has_new_code = new_sha != old_sha

    # 4. Validate import (only when new code was pulled)
    if has_new_code:
        validate = subprocess.run(
            ["uv", "run", "python", "-c", "import pynchy"],
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
            chat_jid = deps.god_chat_jid()
            if chat_jid:
                msg = f"Deploy failed — import validation error, rolled back to {old_sha[:8]}."
                await deps.broadcast_host_message(chat_jid, msg)
            return web.json_response(
                {"error": "import validation failed", "rolled_back_to": old_sha},
                status=422,
            )

    # 5. Rebuild container image if container/ files changed
    if has_new_code:
        container_diff = subprocess.run(
            ["git", "diff", "--name-only", old_sha, new_sha, "--", "container/"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if container_diff.stdout.strip():
            build_script = PROJECT_ROOT / "container" / "build.sh"
            logger.info("Container files changed, rebuilding image...")
            result = subprocess.run(
                [str(build_script)],
                cwd=str(PROJECT_ROOT / "container"),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                logger.error(
                    "Container rebuild failed",
                    stderr=result.stderr[-500:],
                )
                chat_jid = deps.god_chat_jid()
                if chat_jid:
                    msg = "Deploy warning — container rebuild failed, continuing with old image."
                    await deps.broadcast_host_message(chat_jid, msg)
            else:
                logger.info("Container image rebuilt successfully")

    # 6. Restart (write continuation only when new code was deployed)
    chat_jid = deps.god_chat_jid()
    if has_new_code:
        await finalize_deploy(
            broadcast_host_message=deps.broadcast_host_message,
            chat_jid=chat_jid,
            commit_sha=new_sha,
            previous_sha=old_sha,
            sigterm_delay=0.5,
        )
    else:
        # Plain restart — no continuation needed, boot notification handles "I'm back"
        logger.info("Restarting service (no new code)")
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

    return web.json_response(
        {
            "status": "restarting",
            "sha": new_sha,
            "commit": _get_head_commit_message(),
            "dirty": _is_repo_dirty(),
            "previous_sha": old_sha,
        }
    )


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


async def _handle_api_periodic(request: web.Request) -> web.Response:
    """Return periodic agent status."""
    deps: HttpDeps = request.app["deps"]
    agents = await deps.get_periodic_agents()
    return web.json_response(agents)


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
    app.router.add_get("/api/periodic", _handle_api_periodic)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DEPLOY_PORT)
    await site.start()
    logger.info("HTTP server listening", port=DEPLOY_PORT)
    return runner
