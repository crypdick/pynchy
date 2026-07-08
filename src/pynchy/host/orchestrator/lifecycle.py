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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from pynchy.config import get_settings
from pynchy.host.orchestrator import startup_handler
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.messaging import router as output_handler
from pynchy.host.orchestrator.messaging.inbound import start_message_loop
from pynchy.logger import logger
from pynchy.plugins.channel_runtime import (
    ChannelPluginContext,
    load_channels,
    resolve_default_channel,
)
from pynchy.state import init_database, store_chat_metadata
from pynchy.types import OutboundEvent, OutboundEventType
from pynchy.utils import create_background_task

if TYPE_CHECKING:
    from pynchy.types import NewMessage


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

_SHUTDOWN_HARD_EXIT_SECONDS = 60


async def shutdown_app(app: PynchyApp, sig_name: str, *, exit_process: bool = False) -> None:
    """Graceful shutdown handler.  Second signal force-exits."""
    if app._shutting_down:
        logger.info("Force shutdown")
        os._exit(1)
    app._shutting_down = True
    logger.info("Shutdown signal received", signal=sig_name)

    # Hard-exit watchdog: if graceful shutdown hangs, force-exit within
    # systemd's 90s stop timeout. Keep this larger than the container stop
    # budget: one graceful Docker stop can consume roughly 12s by itself.
    watchdog = threading.Timer(_SHUTDOWN_HARD_EXIT_SECONDS, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()

    try:
        # Notify the admin group that the service is going down.
        try:
            from pynchy.host.orchestrator.adapters import find_admin_jid

            admin_jid = find_admin_jid(app.workspaces) or None
            if admin_jid and app.channels:
                await app.broadcast_host_message(admin_jid, f"Shutting down ({sig_name})")
        except Exception:
            logger.debug("Shutdown notification failed", exc_info=True)

        # Cancel subsystem tasks first — prevents scheduler/IPC from creating
        # more work while we're shutting down.
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
    finally:
        watchdog.cancel()

    if exit_process:
        os._exit(0)


# ---------------------------------------------------------------------------
# Phase 1: Core initialization
# ---------------------------------------------------------------------------


async def _initialize_core(app: PynchyApp) -> None:
    """Plugins, gateway, database, observers, memory, state."""
    from pynchy.host.orchestrator.service_installer import install_service
    from pynchy.host.orchestrator.workspace_config import configure_plugin_workspaces
    from pynchy.plugins import get_plugin_manager
    from pynchy.plugins.runtimes.system_checks import ensure_container_system_running

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

    def dispatch_inbound(jid: str, msg: NewMessage) -> None:
        create_background_task(app._on_inbound(jid, msg), name="on-inbound")

    def dispatch_chat_metadata(jid: str, ts: str, name: str | None = None) -> None:
        create_background_task(store_chat_metadata(jid, ts, name), name="store-metadata")

    async def send_text_message(jid: str, text: str) -> None:
        """Adapter for the documented plugin contract (docs/plugins/hooks.md):
        send_message takes plain text, not the internal OutboundEvent type.
        """
        await app.broadcast_to_channels(
            jid, OutboundEvent(type=OutboundEventType.TEXT, content=text)
        )

    def dispatch_reaction(jid: str, ts: str, user: str, emoji: str) -> None:
        create_background_task(app._on_reaction(jid, ts, user, emoji), name="on-reaction")

    def dispatch_ask_user_answer(request_id: str, answer: dict[str, Any]) -> None:
        create_background_task(
            app._on_ask_user_answer(request_id, answer), name="on-ask-user-answer"
        )

    context = ChannelPluginContext(
        on_message_callback=dispatch_inbound,
        on_chat_metadata_callback=dispatch_chat_metadata,
        workspaces=lambda: app.workspaces,
        send_message=send_text_message,
        on_reaction_callback=dispatch_reaction,
        on_ask_user_answer_callback=dispatch_ask_user_answer,
    )
    assert app.plugin_manager is not None, (
        "phase 1 (_initialize_core) must run before _setup_channels"
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
    from pynchy.host.git_ops.worktree import reconcile_worktrees_at_startup
    from pynchy.host.orchestrator.workspace_config import (
        get_repo_access_groups,
        reconcile_workspaces,
    )

    s = get_settings()

    repo_groups = get_repo_access_groups(s.workspaces)

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
    from pynchy.host.container_manager.ipc import start_ipc_watcher
    from pynchy.host.git_ops.repo import get_repo_context
    from pynchy.host.git_ops.sync_poll import (
        start_external_repo_sync_loop,
        start_host_git_sync_loop,
    )
    from pynchy.host.orchestrator.dep_factory import (
        make_git_sync_deps,
        make_http_deps,
        make_ipc_deps,
        make_status_deps,
    )
    from pynchy.host.orchestrator.http_server import start_http_server
    from pynchy.host.orchestrator.status import record_start_time
    from pynchy.host.orchestrator.task_scheduler import SchedulerDependencies, start_scheduler_loop
    from pynchy.plugins.tunnels import check_tunnels

    s = get_settings()

    scheduler_deps = cast(SchedulerDependencies, app)
    app._subsystem_tasks.append(
        create_background_task(start_scheduler_loop(scheduler_deps), name="scheduler")
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

    assert app.plugin_manager is not None, (
        "phase 1 (_initialize_core) must run before _start_subsystems"
    )
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

    shutdown_task: asyncio.Task[Any] | None = None

    def make_shutdown_handler(s: signal.Signals) -> Callable[[], None]:
        def handler() -> None:
            nonlocal shutdown_task
            shutdown_task = create_background_task(
                shutdown_app(app, s.name, exit_process=True),
                name=f"shutdown-{s.name}",
            )

        return handler

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, make_shutdown_handler(sig))

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
    try:
        await start_message_loop(app, lambda: app._shutting_down)
    finally:
        if shutdown_task is not None and not shutdown_task.done():
            await asyncio.shield(shutdown_task)
