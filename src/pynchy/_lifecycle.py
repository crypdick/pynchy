"""Application lifecycle — startup phases, signal handling, shutdown.

Extracted from ``app.py`` to keep the orchestrator focused on state
management and delegation.  Each function receives the ``PynchyApp``
instance so it can access runtime state without being a method.

Startup runs in five explicit phases (see :func:`run_app`).
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import threading
from typing import TYPE_CHECKING

from pynchy import startup_handler
from pynchy.chat import output_handler
from pynchy.chat._message_routing import start_message_loop
from pynchy.plugins.channel_runtime import (
    ChannelPluginContext,
    load_channels,
    resolve_default_channel,
)
from pynchy.config import get_settings
from pynchy.state import init_database, store_chat_metadata
from pynchy.logger import logger
from pynchy.utils import create_background_task

if TYPE_CHECKING:
    from pynchy.app import PynchyApp


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def shutdown_app(app: PynchyApp, sig_name: str) -> None:
    """Graceful shutdown handler.  Second signal force-exits."""
    if app._shutting_down:
        logger.info("Force shutdown")
        os._exit(1)
    app._shutting_down = True
    logger.info("Shutdown signal received", signal=sig_name)

    # Hard-exit watchdog: if graceful shutdown hangs, force-exit after 12s.
    watchdog = threading.Timer(12, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()

    # Notify the admin group that the service is going down.
    try:
        from pynchy.adapters import find_admin_jid

        admin_jid = find_admin_jid(app.workspaces) or None
        if admin_jid and app.channels:
            await app.broadcast_host_message(admin_jid, f"Shutting down ({sig_name})")
    except Exception:
        logger.debug("Shutdown notification failed", exc_info=True)

    # Cancel subsystem tasks first — prevents scheduler/IPC from creating
    # new work while we're shutting down.
    for task in app._subsystem_tasks:
        task.cancel()
    app._subsystem_tasks.clear()

    # Suppress reconnect attempts before cleanup.
    for ch in app.channels:
        ch.prepare_shutdown()

    if app._http_runner:
        await asyncio.sleep(0.3)
        await app._http_runner.cleanup()

    await app.queue.shutdown()

    from pynchy.host.container_manager.gateway import stop_gateway

    await stop_gateway()
    for obs in app._observers:
        await obs.close()
    if app._memory:
        await app._memory.close()
    batcher = output_handler.get_trace_batcher()
    if batcher is not None:
        await batcher.flush_all()
    for ch in app.channels:
        await ch.disconnect()


# ---------------------------------------------------------------------------
# Phase 1: Core initialization
# ---------------------------------------------------------------------------


async def _initialize_core(app: PynchyApp) -> None:
    """Plugins, gateway, database, observers, memory, state."""
    from pynchy.plugins import get_plugin_manager
    from pynchy.plugins.runtimes.system_checks import ensure_container_system_running
    from pynchy.service_installer import install_service
    from pynchy.workspace_config import configure_plugin_workspaces

    install_service()

    app.plugin_manager = get_plugin_manager()
    configure_plugin_workspaces(app.plugin_manager)
    ensure_container_system_running()

    from pynchy.host.container_manager.gateway import start_gateway

    await start_gateway(plugin_manager=app.plugin_manager)

    await init_database()
    logger.info("Database initialized")

    from pynchy.plugins.memory import get_memory_provider
    from pynchy.plugins.observers import attach_observers

    app._observers = attach_observers(app.event_bus)

    app._memory = get_memory_provider()
    if app._memory:
        await app._memory.init()

    await app._load_state()


# ---------------------------------------------------------------------------
# Phase 2: Channel setup
# ---------------------------------------------------------------------------


async def _setup_channels(app: PynchyApp) -> None:
    """Create channel context, load channels, validate, connect."""
    context = ChannelPluginContext(
        on_message_callback=lambda jid, msg: create_background_task(
            app._on_inbound(jid, msg), name="on-inbound"
        ),
        on_chat_metadata_callback=lambda jid, ts, name=None: create_background_task(
            store_chat_metadata(jid, ts, name), name="store-metadata"
        ),
        workspaces=lambda: app.workspaces,
        send_message=app.broadcast_to_channels,
        on_reaction_callback=lambda jid, ts, user, emoji: create_background_task(
            app._on_reaction(jid, ts, user, emoji), name="on-reaction"
        ),
        on_ask_user_answer_callback=lambda request_id, answer: create_background_task(
            app._on_ask_user_answer(request_id, answer), name="on-ask-user-answer"
        ),
    )
    app.channels = load_channels(app.plugin_manager, context)
    for ch in app.channels:
        missing = startup_handler.validate_plugin_credentials(ch)
        if missing:
            logger.warning(
                "Channel missing credentials",
                channel=type(ch).__name__,
                missing=missing,
            )
    output_handler.init_trace_batcher(app)

    for ch in app.channels:
        await ch.connect()


# ---------------------------------------------------------------------------
# Phase 3: State reconciliation
# ---------------------------------------------------------------------------


async def _reconcile_state(app: PynchyApp) -> dict[str, list[str]]:
    """Worktree + workspace reconciliation.  Returns repo_groups."""
    from pynchy.git_ops.worktree import reconcile_worktrees_at_startup
    from pynchy.workspace_config import reconcile_workspaces

    s = get_settings()

    repo_groups: dict[str, list[str]] = {}
    for folder, ws_cfg in s.workspaces.items():
        if ws_cfg.repo_access:
            repo_groups.setdefault(ws_cfg.repo_access, []).append(folder)

    await asyncio.to_thread(
        reconcile_worktrees_at_startup,
        repo_groups=repo_groups,
    )

    await reconcile_workspaces(
        workspaces=app.workspaces,
        channels=app.channels,
        register_fn=app._register_workspace,
        unregister_fn=app._unregister_workspace,
    )

    return repo_groups


# ---------------------------------------------------------------------------
# Phase 4: Subsystem startup
# ---------------------------------------------------------------------------


async def _start_subsystems(app: PynchyApp, repo_groups: dict[str, list[str]]) -> None:
    """Scheduler, IPC, git sync, HTTP server."""
    from pynchy.dep_factory import (
        make_git_sync_deps,
        make_http_deps,
        make_ipc_deps,
        make_scheduler_deps,
        make_status_deps,
    )
    from pynchy.git_ops.repo import get_repo_context
    from pynchy.git_ops.sync_poll import (
        start_external_repo_sync_loop,
        start_host_git_sync_loop,
    )
    from pynchy.http_server import start_http_server
    from pynchy.host.container_manager.ipc import start_ipc_watcher
    from pynchy.status import record_start_time
    from pynchy.task_scheduler import start_scheduler_loop
    from pynchy.plugins.tunnels import check_tunnels

    s = get_settings()

    app._subsystem_tasks.append(
        create_background_task(start_scheduler_loop(make_scheduler_deps(app)), name="scheduler")
    )
    app._subsystem_tasks.append(
        create_background_task(start_ipc_watcher(make_ipc_deps(app)), name="ipc-watcher")
    )
    app._subsystem_tasks.append(
        create_background_task(start_host_git_sync_loop(make_git_sync_deps(app)), name="git-sync")
    )

    for slug, _folders in repo_groups.items():
        repo_ctx = get_repo_context(slug)
        if repo_ctx and repo_ctx.root.resolve() != s.project_root.resolve():
            app._subsystem_tasks.append(
                create_background_task(
                    start_external_repo_sync_loop(repo_ctx, make_git_sync_deps(app)),
                    name=f"git-sync-{slug}",
                )
            )
    app.queue.set_process_messages_fn(app._process_group_messages)

    check_tunnels(app.plugin_manager)
    record_start_time()
    app._http_runner = await start_http_server(
        make_http_deps(app), status_deps=make_status_deps(app)
    )

    hostname = socket.gethostname()
    logger.info(
        "HTTP server ready",
        port=s.server.port,
        local=f"http://localhost:{s.server.port}/status",
        remote=f"http://{hostname}:{s.server.port}/status",
    )


# ---------------------------------------------------------------------------
# Run — top-level orchestrator
# ---------------------------------------------------------------------------


async def run_app(app: PynchyApp) -> None:
    """Main entry point — startup sequence.

    Phases:
    1. Core initialization (plugins, gateway, DB, observers, state)
    2. Channel setup (load, validate, connect)
    3. State reconciliation (worktrees, workspaces)
    4. Subsystem startup (scheduler, IPC, git sync, HTTP)
    5. Boot finalization (notification, recovery, message loop)
    """
    s = get_settings()
    continuation_path = s.data_dir / "deploy_continuation.json"

    try:
        await _initialize_core(app)
    except Exception as exc:
        if continuation_path.exists():
            await startup_handler.auto_rollback(continuation_path, exc)
        raise

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.ensure_future(shutdown_app(app, s.name)),
        )

    try:
        await _setup_channels(app)
    except Exception as exc:
        if continuation_path.exists():
            await startup_handler.auto_rollback(continuation_path, exc)
        raise

    if not app.workspaces:
        default_channel = resolve_default_channel(app.channels)
        await startup_handler.setup_admin_group(app, default_channel)

    repo_groups = await _reconcile_state(app)
    await _start_subsystems(app, repo_groups)

    await startup_handler.send_boot_notification(app)
    await app._catch_up_channel_history()
    await startup_handler.recover_pending_messages(app)
    await startup_handler.check_deploy_continuation(app)

    if app.message_loop_running:
        logger.debug("Message loop already running, skipping duplicate start")
        return
    app.message_loop_running = True
    await start_message_loop(app, lambda: app._shutting_down)
