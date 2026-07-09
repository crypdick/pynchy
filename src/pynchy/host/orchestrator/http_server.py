"""Embedded HTTP server for health checks, remote deploys, and TUI API.

Exposes endpoints on 0.0.0.0:DEPLOY_PORT. Access is controlled by
Tailscale ACLs and the machine firewall.
"""

from __future__ import annotations

import asyncio
import json
import subprocess  # noqa: S404, RUF100 - deploy validation uses fixed no-shell uv argv.
import time
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves HTTP dependency annotations at runtime.
    Coroutine,  # noqa: TC003, RUF100 - beartype resolves HTTP dependency annotations at runtime.
)
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from aiohttp import web

from pynchy.config import get_settings
from pynchy.host.git_ops.utils import (
    files_changed_between,
    get_head_commit_message,
    get_head_sha,
    is_repo_dirty,
    push_local_commits,
    run_git,
)
from pynchy.host.orchestrator.status import StatusDeps, collect_status
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.host.orchestrator.temporal.scheduler import start_deploy_workflow
from pynchy.logger import logger
from pynchy.types import (
    NewMessage,  # noqa: TC001, RUF100 - beartype resolves HTTP dependency annotations at runtime.
)

if TYPE_CHECKING:
    from aiohttp.web_app import Application as AiohttpApplication
else:
    AiohttpApplication = object

_start_time = time.monotonic()
REMOTE_HTTP_BIND_HOST = "0.0.0.0"  # noqa: S104, RUF100 - documented Tailscale/firewall-gated API listener for remote clients.

# Typed app key avoids aiohttp NotAppKeyWarning from plain-string lookups.
deps_key: web.AppKey[HttpDeps] = web.AppKey("deps")
status_deps_key: web.AppKey[StatusDeps] = web.AppKey("status_deps")


def _write_boot_warning(message: str) -> None:
    """Append a warning to boot_warnings.json, picked up by _send_boot_notification on restart."""
    path = get_settings().data_dir / "boot_warnings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        warnings = json.loads(path.read_text()) if path.exists() else []
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read boot warnings file, starting fresh", err=str(exc))
        warnings = []
    warnings.append(message)
    path.write_text(json.dumps(warnings))


@runtime_checkable
class HttpDeps(Protocol):
    """Dependencies injected by app.py."""

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    def admin_chat_jid(self) -> str: ...

    def channels_connected(self) -> bool: ...

    # --- TUI API deps ---

    def get_groups(self) -> list[dict[str, Any]]: ...

    async def get_messages(self, jid: str, limit: int) -> list[NewMessage]: ...

    async def send_user_message(self, jid: str, content: str) -> None: ...

    def subscribe_events(
        self, callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
    ) -> Callable[[], None]: ...

    async def get_periodic_agents(self) -> list[dict[str, Any]]: ...

    def get_active_sessions(self) -> dict[str, str]: ...

    def is_shutting_down(self) -> bool: ...


# ------------------------------------------------------------------
# Existing endpoints
# ------------------------------------------------------------------


async def _handle_health(request: web.Request) -> web.Response:  # noqa: RUF029, RUF100 - aiohttp route handlers are async.
    deps: HttpDeps = request.app[deps_key]
    return web.json_response(
        {
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - _start_time),
            "head_sha": get_head_sha(),
            "head_commit": get_head_commit_message(),
            "dirty": is_repo_dirty(),
            "channels_connected": deps.channels_connected(),
        }
    )


async def _handle_deploy(request: web.Request) -> web.Response:
    deps: HttpDeps = request.app[deps_key]
    old_sha = get_head_sha()

    # 1. Push any local commits before pulling (prevents divergence)
    run_git("fetch", "origin")
    if not push_local_commits(skip_fetch=True):
        logger.warning("Pre-deploy push failed, continuing with rebase")

    # 2. Stash dirty files so they don't block the rebase
    stash = run_git("stash")
    stashed = stash.returncode == 0 and "No local changes" not in stash.stdout

    # 3. Rebase to incorporate incoming remote changes
    pull = run_git("rebase", "origin/main")
    if pull.returncode != 0:
        # Abort failed rebase to leave repo clean, then continue with current code
        run_git("rebase", "--abort")
        logger.warning(
            "git rebase failed, restarting with current code", stderr=pull.stderr.strip()
        )
        _write_boot_warning(
            "Deploy rolled back to previous commit because incoming commits failed to rebase. "
            "Please reconcile the incoming changes into your local clone, push, then redeploy."
        )

    # Restore stashed files regardless of rebase outcome
    if stashed:
        run_git("stash", "pop")

    new_sha = get_head_sha()
    has_new_code = new_sha != old_sha

    # 4. Validate import (only when the pull changed HEAD)
    if has_new_code:
        s = get_settings()
        validate = await asyncio.to_thread(
            subprocess.run,
            ["uv", "run", "python", "-c", "import pynchy"],
            cwd=str(s.project_root),
            capture_output=True,
            text=True,
        )
        if validate.returncode != 0:
            err = validate.stderr.strip()[-300:]
            logger.error("Deploy validation failed, rolling back", error=err)
            run_git("reset", "--hard", old_sha)
            chat_jid = deps.admin_chat_jid()
            if chat_jid:
                msg = f"Deploy failed — import validation error, rolled back to {old_sha[:8]}."
                await deps.broadcast_host_message(chat_jid, msg)
            return web.json_response(
                {"error": "import validation failed", "rolled_back_to": old_sha},
                status=422,
            )

    # 5. Start deploy workflow. The activity owns rebuild, continuation, and restart.
    chat_jid = deps.admin_chat_jid()
    rebuild = has_new_code and files_changed_between(old_sha, new_sha, "src/pynchy/agent/")
    await start_deploy_workflow(
        DeployRequest(
            chat_jid=chat_jid,
            commit_sha=new_sha,
            previous_sha=old_sha,
            active_sessions=deps.get_active_sessions(),
            rebuild=rebuild,
            reason="http",
        )
    )

    return web.json_response(
        {
            "status": "restarting",
            "sha": new_sha,
            "commit": get_head_commit_message(),
            "dirty": is_repo_dirty(),
            "previous_sha": old_sha,
        }
    )


async def _handle_status(request: web.Request) -> web.Response:
    """Comprehensive operational status from all subsystems."""
    status_deps: StatusDeps = request.app[status_deps_key]
    data = await collect_status(status_deps, _start_time)
    return web.json_response(data)


# ------------------------------------------------------------------
# TUI API endpoints
# ------------------------------------------------------------------


async def _handle_api_groups(request: web.Request) -> web.Response:  # noqa: RUF029, RUF100 - aiohttp route handlers are async.
    """Return registered groups."""
    deps: HttpDeps = request.app[deps_key]
    return web.json_response(deps.get_groups())


async def _handle_api_messages(request: web.Request) -> web.Response:
    """Return chat history for a group."""
    deps: HttpDeps = request.app[deps_key]
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
    deps: HttpDeps = request.app[deps_key]
    body = await request.json()
    jid = body.get("jid", "")
    content = body.get("content", "")
    if not jid or not content:
        return web.json_response({"error": "jid and content required"}, status=400)
    await deps.send_user_message(jid, content)
    return web.json_response({"status": "ok"})


async def _stream_sse_events(
    response: web.StreamResponse,
    queue: asyncio.Queue[dict[str, Any]],
    deps: HttpDeps,
) -> None:
    while True:
        if deps.is_shutting_down():
            return
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.2)
        except TimeoutError:
            continue
        data = json.dumps(event)
        await response.write(f"data: {data}\n\n".encode())


async def _handle_api_events(request: web.Request) -> web.StreamResponse:
    """SSE stream for real-time events (messages, agent activity)."""
    deps: HttpDeps = request.app[deps_key]

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
        await _stream_sse_events(response, queue, deps)
    except (asyncio.CancelledError, ConnectionResetError):
        pass  # Client disconnected or request cancelled — clean up silently
    finally:
        unsubscribe()

    return response


async def _handle_api_periodic(request: web.Request) -> web.Response:
    """Return periodic agent status."""
    deps: HttpDeps = request.app[deps_key]
    agents = await deps.get_periodic_agents()
    return web.json_response(agents)


# ------------------------------------------------------------------
# Server setup
# ------------------------------------------------------------------


async def start_http_server(
    deps: HttpDeps, *, status_deps: StatusDeps | None = None
) -> web.AppRunner:
    """Create, start, and return the HTTP server runner."""
    app = create_http_app(deps, status_deps=status_deps)

    runner = web.AppRunner(app)
    await runner.setup()
    port = get_settings().server.port
    site = web.TCPSite(runner, REMOTE_HTTP_BIND_HOST, port)
    await site.start()
    logger.info("HTTP server listening", port=port)
    return runner


def create_http_app(deps: HttpDeps, *, status_deps: StatusDeps | None = None) -> AiohttpApplication:
    """Build the aiohttp app with all HTTP routes registered."""
    app = web.Application()
    app[deps_key] = deps
    if status_deps is not None:
        app[status_deps_key] = status_deps
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/status", _handle_status)
    app.router.add_post("/deploy", _handle_deploy)
    app.router.add_get("/api/groups", _handle_api_groups)
    app.router.add_get("/api/messages", _handle_api_messages)
    app.router.add_post("/api/send", _handle_api_send)
    app.router.add_get("/api/events", _handle_api_events)
    app.router.add_get("/api/periodic", _handle_api_periodic)
    return app
