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
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves lifecycle annotations at runtime.
)
from typing import Any, cast

import pluggy  # noqa: TC002, RUF100 - beartype resolves plugin-manager annotations at runtime.

from pynchy.config import get_settings
from pynchy.host.container_manager import gateway as gateway_manager
from pynchy.host.container_manager import ipc as ipc_manager
from pynchy.host.git_ops import worktree as worktree_ops
from pynchy.host.orchestrator import adapters as orchestrator_adapters
from pynchy.host.orchestrator import (
    dep_factory,
    http_server,
    service_installer,
    startup_handler,
    status,
    task_scheduler,
    workspace_config,
)
from pynchy.host.orchestrator.app import (  # noqa: TC001, RUF100 - beartype resolves lifecycle annotations at runtime.
    PynchyApp,
)
from pynchy.host.orchestrator.messaging import approval_handler
from pynchy.host.orchestrator.messaging import router as output_handler
from pynchy.host.orchestrator.messaging.inbound import start_message_loop
from pynchy.logger import logger
from pynchy.plugins import get_plugin_manager
from pynchy.plugins import memory as memory_plugins
from pynchy.plugins import observers as observer_plugins
from pynchy.plugins import speech as speech_plugins
from pynchy.plugins import tunnels as tunnel_plugins
from pynchy.plugins.channel_runtime import (
    ChannelPluginContext,
    load_channels,
    resolve_default_channel,
)
from pynchy.plugins.integrations import linear_boot
from pynchy.plugins.runtimes import system_checks
from pynchy.state import init_database, store_chat_metadata
from pynchy.types import NewMessage, OutboundEvent, OutboundEventType
from pynchy.utils import create_background_task

# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

_SHUTDOWN_HARD_EXIT_SECONDS = 60
_PLUGIN_MANAGER_NOT_INITIALIZED = "phase 1 (_initialize_core) must run before {phase}"


def _require_plugin_manager(app: PynchyApp, phase: str) -> pluggy.PluginManager:
    if app.plugin_manager is None:
        raise RuntimeError(_PLUGIN_MANAGER_NOT_INITIALIZED.format(phase=phase))
    return app.plugin_manager


def _start_shutdown_watchdog() -> object:
    watchdog = threading.Timer(_SHUTDOWN_HARD_EXIT_SECONDS, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()
    return watchdog


async def _notify_admin_shutdown(app: PynchyApp, sig_name: str) -> None:
    try:
        admin_jid = (
            orchestrator_adapters.resolve_admin_notification_jid(
                app.workspaces, get_settings().notifications.admin_workspace
            )
            or None
        )
        if admin_jid and app.channels:
            await app.broadcast_host_message(admin_jid, f"Shutting down ({sig_name})")
    except Exception:  # noqa: BLE001, RUF100 - shutdown notification is best-effort and must not block teardown.
        logger.debug("Shutdown notification failed", exc_info=True)


def _cancel_subsystem_tasks(app: PynchyApp) -> None:
    app.cancel_subsystem_tasks()


def _prepare_channels_for_shutdown(app: PynchyApp) -> None:
    for channel in app.channels:
        channel.prepare_shutdown()


async def _cleanup_http_runner(app: PynchyApp) -> None:
    await asyncio.sleep(0.3)
    await app.cleanup_http_runner()


async def _close_runtime_resources(app: PynchyApp) -> None:
    await gateway_manager.stop_gateway()
    await app.close_observers()
    await app.close_memory_provider()
    batcher = output_handler.get_trace_batcher()
    if batcher is not None:
        await batcher.flush_all()
    for channel in app.channels:
        await channel.disconnect()


async def shutdown_app(app: PynchyApp, sig_name: str, *, exit_process: bool = False) -> None:
    """Graceful shutdown handler.  Second signal force-exits."""
    if not app.begin_shutdown():
        logger.info("Force shutdown")
        os._exit(1)
    logger.info("Shutdown signal received", signal=sig_name)

    # Hard-exit watchdog: if graceful shutdown hangs, force-exit within
    # systemd's 90s stop timeout. Keep this larger than the container stop
    # budget: one graceful Docker stop can consume roughly 12s by itself.
    watchdog = _start_shutdown_watchdog()

    try:
        await _notify_admin_shutdown(app, sig_name)
        _cancel_subsystem_tasks(app)
        _prepare_channels_for_shutdown(app)
        await _cleanup_http_runner(app)

        await app.queue.shutdown()
        await _close_runtime_resources(app)
    finally:
        watchdog.cancel()

    if exit_process:
        os._exit(0)


# ---------------------------------------------------------------------------
# Phase 1: Core initialization
# ---------------------------------------------------------------------------


async def _initialize_core(app: PynchyApp) -> None:
    """Plugins, gateway, database, observers, memory, state."""
    service_installer.install_service()

    app.plugin_manager = get_plugin_manager()
    app.set_speech_synthesizer(speech_plugins.get_speech_synthesizer(app.plugin_manager))
    workspace_config.configure_plugin_workspaces(app.plugin_manager)
    system_checks.ensure_container_system_running()

    await gateway_manager.start_gateway(plugin_manager=app.plugin_manager)

    await init_database()
    logger.info("Database initialized")

    app.attach_observers(observer_plugins.attach_observers(app.event_bus))

    await app.set_memory_provider(memory_plugins.get_memory_provider())
    await app.load_state()


# ---------------------------------------------------------------------------
# Phase 2: Channel setup
# ---------------------------------------------------------------------------


async def _setup_channels(app: PynchyApp) -> None:
    """Create channel context, load channels, validate, connect."""

    def dispatch_inbound(jid: str, msg: NewMessage) -> None:
        create_background_task(app.on_inbound(jid, msg), name="on-inbound")

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
        create_background_task(app.on_reaction(jid, ts, user, emoji), name="on-reaction")

    def dispatch_ask_user_answer(request_id: str, answer: dict[str, Any]) -> None:
        create_background_task(
            app.on_ask_user_answer(request_id, answer), name="on-ask-user-answer"
        )

    def dispatch_approval_decision(chat_jid: str, action: str, short_id: str, sender: str) -> None:
        create_background_task(
            approval_handler.handle_approval_command(app, chat_jid, action, short_id, sender),
            name="on-approval-decision",
        )

    context = ChannelPluginContext(
        on_message_callback=dispatch_inbound,
        on_chat_metadata_callback=dispatch_chat_metadata,
        workspaces=lambda: app.workspaces,
        send_message=send_text_message,
        on_reaction_callback=dispatch_reaction,
        on_ask_user_answer_callback=dispatch_ask_user_answer,
        on_approval_decision_callback=dispatch_approval_decision,
        speech_synthesizer=app.get_speech_synthesizer(),
    )
    plugin_manager = _require_plugin_manager(app, "_setup_channels")
    app.channels = load_channels(plugin_manager, context)
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
    s = get_settings()

    repo_groups = workspace_config.get_repo_access_groups(s.workspaces)

    await asyncio.to_thread(
        worktree_ops.reconcile_worktrees_at_startup,
        repo_groups=repo_groups,
    )

    await workspace_config.reconcile_workspaces(
        workspaces=app.workspaces,
        channels=app.channels,
        register_fn=app.register_workspace,
        unregister_fn=app.unregister_workspace,
    )

    await linear_boot.reconcile_linear_workspace_boards(app.workspaces.values())

    return repo_groups


# ---------------------------------------------------------------------------
# Phase 4: Subsystem startup
# ---------------------------------------------------------------------------


async def _start_subsystems(app: PynchyApp, _repo_groups: dict[str, list[str]]) -> None:
    """Scheduler, IPC, git sync, HTTP server."""
    s = get_settings()

    scheduler_deps = cast("task_scheduler.SchedulerDependencies", app)
    app.add_subsystem_task(
        create_background_task(
            task_scheduler.start_scheduler_loop(scheduler_deps), name="scheduler"
        )
    )
    app.add_subsystem_task(
        create_background_task(
            ipc_manager.start_ipc_watcher(dep_factory.make_ipc_deps(app)),
            name="ipc-watcher",
        )
    )

    app.queue.set_process_messages_fn(app.process_group_messages)

    plugin_manager = _require_plugin_manager(app, "_start_subsystems")
    tunnel_plugins.check_tunnels(plugin_manager)
    status.record_start_time()
    app.set_http_runner(
        await http_server.start_http_server(
            dep_factory.make_http_deps(app),
            status_deps=dep_factory.make_status_deps(app),
        )
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
    except Exception as exc:  # noqa: BLE001, RUF100 - startup rollback boundary; any init failure should trigger rollback.
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
    except Exception as exc:  # noqa: BLE001, RUF100 - startup rollback boundary; any channel setup failure should trigger rollback.
        if continuation_path.exists():
            await startup_handler.auto_rollback(continuation_path, exc)
        raise

    if not app.workspaces:
        default_channel = resolve_default_channel(app.channels)
        await startup_handler.setup_admin_group(app, default_channel)

    repo_groups = await _reconcile_state(app)
    interrupted_recovery = await startup_handler.prepare_interrupted_turn_recovery()
    await _start_subsystems(app, repo_groups)

    await startup_handler.send_boot_notification(app)
    await app.catch_up_channels()
    recovering_chats = await startup_handler.dispatch_interrupted_turn_recovery(
        app,
        interrupted_recovery,
    )
    await startup_handler.recover_pending_messages(app, exclude_chat_jids=recovering_chats)

    if app.message_loop_running:
        logger.debug("Message loop already running, skipping duplicate start")
        return
    app.message_loop_running = True
    try:
        await start_message_loop(app, app.is_shutting_down)
    finally:
        if shutdown_task is not None and not shutdown_task.done():
            await asyncio.shield(shutdown_task)
