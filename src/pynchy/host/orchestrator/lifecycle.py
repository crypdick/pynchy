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
import threading
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves lifecycle annotations at runtime.
)
from pathlib import Path  # beartype resolves lifecycle annotations.
from typing import Any

import pynchy.plugins.speech.api as speech_plugins
import pynchy.plugins.tunnels.api as tunnel_plugins
from pynchy.async_tasks import create_background_task
from pynchy.channels import SlackConnectionSettings, WhatsAppConnectionSettings
from pynchy.config.api import get_settings
from pynchy.host.audio import process_inbound_audio_attachments, transcribe_audio_file
from pynchy.host.container_manager import gateway as gateway_manager
from pynchy.host.container_manager.ipc.bootstrap import register_builtin_handlers
from pynchy.host.container_manager.ipc.watcher import start_ipc_watcher
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.git_ops.api import reconcile_worktrees_at_startup
from pynchy.host.orchestrator import adapters as orchestrator_adapters
from pynchy.host.orchestrator import (
    dep_factory,
    http_server,
    job_sources,
    linear_issue_controls,
    plugin_configuration,
    routed_workspace_policy,
    service_installer,
    startup_handler,
    status,
    task_scheduler,
    workspace_config,
)
from pynchy.host.orchestrator.app import (  # noqa: TC001 - beartype resolves lifecycle annotations at runtime.
    PynchyApp,
)
from pynchy.host.orchestrator.deploy import current_deploy_revision
from pynchy.host.orchestrator.http_control import resolve_control_plane_runtime
from pynchy.host.orchestrator.messaging import approval_handler
from pynchy.host.orchestrator.messaging import router as output_handler
from pynchy.host.orchestrator.messaging.inbound import start_message_loop
from pynchy.host.orchestrator.scheduled_binding import reconcile_scheduled_task_bindings
from pynchy.identifiers import OrphanReapAgeMs
from pynchy.logger import logger
from pynchy.plugins.api import (
    ChannelPluginContext,
    ConnectionRuntimeContext,
    NewMessage,
    OutboundEvent,
    OutboundEventType,
    attach_observers,
    get_plugin_manager,
    initialize_host_action_catalog,
    load_channels,
    load_connection_runtimes,
    resolve_default_channel,
)
from pynchy.plugins.integrations.github_webhook_models import GitHubPluginOptions
from pynchy.plugins.integrations.github_webhooks import github_webhook_routes
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001 - beartype resolves lifecycle annotations at runtime.
    LinearWorkspaceBoard,
)
from pynchy.plugins.integrations.linear_boot import (
    reconcile_linear_workspace_boards as reconcile_linear_boards,
)
from pynchy.plugins.runtimes import system_checks
from pynchy.state.api import (
    get_all_tasks,
    get_chat_jids_by_name,
    get_last_group_sync,
    init_database,
    initialize_deployment_state,
    recover_incomplete_action_intents,
    recover_incomplete_webhook_effects,
    set_last_group_sync,
    store_chat_metadata,
    update_chat_name,
)
from pynchy.state.connection import StateRuntimeConfig

# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

_SHUTDOWN_HARD_EXIT_SECONDS = 60


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
    except Exception:  # noqa: BLE001 - shutdown notification is best-effort and must not block teardown.
        logger.debug("Shutdown notification failed", exc_info=True)


async def _cleanup_http_runner(app: PynchyApp) -> None:
    await asyncio.sleep(0.3)
    await app.cleanup_http_runner()


async def _close_runtime_resources(app: PynchyApp) -> None:
    await app.connection_runtime_owner.close()
    await app.close_observers()
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
        await app.subsystem_tasks.stop()
        for channel in app.channels:
            channel.prepare_shutdown()
        await _cleanup_http_runner(app)

        try:
            await gateway_manager.stop_gateway()
        finally:
            try:
                await app.queue.shutdown()
            finally:
                await _close_runtime_resources(app)
    finally:
        watchdog.cancel()

    if exit_process:
        os._exit(0)


# ---------------------------------------------------------------------------
# Phase 1: Core initialization
# ---------------------------------------------------------------------------


async def _initialize_core(app: PynchyApp) -> None:
    """Plugins, gateway, database, observers, and state."""
    settings = get_settings()

    service_installer.install_service(settings.project_root)

    app.plugin_manager = get_plugin_manager(
        {name: plugin.enabled for name, plugin in settings.plugins.items()}
    )
    plugin_configuration.configure_startup_plugins(
        app.plugin_manager,
        settings,
        app.start_linear_work_item_reconciliation,
        dep_factory.make_provider_execution_retirement(app),
    )
    initialize_host_action_catalog(app.plugin_manager)
    app.set_speech_synthesizer(speech_plugins.get_speech_synthesizer(app.plugin_manager))
    workspace_config.configure_plugin_workspaces(app.plugin_manager)
    job_sources.configure_plugin_jobs(app.plugin_manager)
    system_checks.ensure_container_system_running(
        OrphanReapAgeMs(settings.container.orphan_reap_age_ms),
        project_root=app.agent_execution_runtime.project_root,
        image=app.agent_execution_runtime.agent_image,
    )

    await gateway_manager.start_gateway(plugin_manager=app.plugin_manager)

    await init_database(StateRuntimeConfig(database_path=settings.data_dir / "messages.db"))
    # A crash can leave an external write without a receipt. Recovery fails closed
    # rather than replaying that side effect.
    await recover_incomplete_action_intents()
    await recover_incomplete_webhook_effects()
    await initialize_deployment_state(current_deploy_revision())
    logger.info("Database initialized")

    app.attach_observers(attach_observers(app.plugin_manager, app.event_bus))
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
        """Adapter for the documented plugin contract (docs/plugins/hooks/index.md):
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

    settings = get_settings()
    whatsapp_connections: dict[str, WhatsAppConnectionSettings] = {}
    for name, config in settings.connections.items():
        if config.type != "whatsapp":
            continue
        if config.auth_db_path:
            auth_db_path = Path(config.auth_db_path)
            if not auth_db_path.is_absolute():
                auth_db_path = settings.project_root / auth_db_path
        else:
            auth_db_path = settings.data_dir / "neonize.db"
        whatsapp_connections[name] = WhatsAppConnectionSettings(
            auth_db_path=auth_db_path.resolve(),
            assistant_name=settings.agent.name,
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
        transcribe_audio=transcribe_audio_file,
        process_inbound_audio=process_inbound_audio_attachments,
        find_chat_jids_by_name=get_chat_jids_by_name,
        get_last_group_sync=get_last_group_sync,
        set_last_group_sync=set_last_group_sync,
        update_chat_name=update_chat_name,
        discord_connections={
            name: config.to_runtime_settings()
            for name, config in settings.connections.items()
            if config.type == "discord"
        },
        discord_audio_cache_dir=settings.data_dir / "media" / "discord",
        slack_connections={
            name: SlackConnectionSettings(
                bot_token_env=config.bot_token_env,
                app_token_env=config.app_token_env,
                chat_names=tuple(config.chat),
                assistant_name=settings.agent.name,
                allow_create=settings.command_center.connection == name,
            )
            for name, config in settings.connections.items()
            if config.type == "slack"
        },
        whatsapp_connections=whatsapp_connections,
    )
    plugin_manager = app.require_plugin_manager("_setup_channels")
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


async def _reconcile_state(app: PynchyApp) -> dict[str, LinearWorkspaceBoard]:
    """Reconcile worktrees and workspaces, returning live Linear board identities."""
    await routed_workspace_policy.restore_routed_workspace_policy_owners(app.workspaces.values())
    repo_groups = workspace_config.get_repo_access_groups(get_settings().workspace_names())

    await asyncio.to_thread(
        reconcile_worktrees_at_startup,
        repo_groups=repo_groups,
    )

    await workspace_config.reconcile_workspaces(
        workspaces=app.workspaces,
        channels=app.channels,
        register_fn=app.register_workspace,
        unregister_fn=app.unregister_workspace,
        rebind_fn=app.rebind_workspace,
        retire_fn=app.retire_workspace_runtime,
    )

    tasks = await get_all_tasks()
    scheduled_bindings = await reconcile_scheduled_task_bindings(tasks, app)
    if scheduled_bindings:
        logger.info("Scheduled task bindings reconciled", count=scheduled_bindings)

    await app.reclaim_orphaned_workspace_artifacts(tasks)

    linear_boards = await reconcile_linear_boards(
        app.workspaces.values(), app.ensure_linear_issue_control
    )
    await linear_issue_controls.ensure_forum_guidelines(app, linear_boards)

    plugin_manager = app.require_plugin_manager("_reconcile_state")
    app.connection_runtime_owner.set(load_connection_runtimes(plugin_manager))

    return dict(linear_boards)


async def start_connection_runtimes(app: PynchyApp) -> None:
    """Start provider polling only after recovery and dispatch are ready."""
    context = ConnectionRuntimeContext(
        channels=lambda: app.channels,
        workspaces=lambda: app.workspaces,
        register_workspace=app.register_workspace,
        unregister_workspace=app.unregister_workspace,
        bind_session=app.bind_routed_session,
        ingest_message=app.on_inbound,
    )
    attempted_runtimes = []
    try:
        for runtime in app.connection_runtime_owner.runtimes():
            attempted_runtimes.append(runtime)
            await runtime.start(context)
    except BaseException:
        for runtime in reversed(attempted_runtimes):
            await runtime.close()
        app.connection_runtime_owner.set([])
        raise


# ---------------------------------------------------------------------------
# Phase 4: Subsystem startup
# ---------------------------------------------------------------------------


async def _prepare_and_bind_control_plane(
    app: PynchyApp,
) -> http_server.PreparedHttpServer:
    """Prepare route callbacks and prove the gated listener can bind."""
    plugin_manager = app.require_plugin_manager("_prepare_and_bind_control_plane")
    tunnel_plugins.check_tunnels(plugin_manager)
    status.record_start_time()
    settings = get_settings()
    server = settings.server
    github_plugin = settings.plugins.get("github")
    github_options = GitHubPluginOptions.model_validate(
        github_plugin.options if github_plugin is not None and github_plugin.enabled else {}
    )
    runtime = resolve_control_plane_runtime(
        bind_host=server.host,
        port=server.port,
        unix_socket=server.unix_socket,
        allow_public_bind=server.allow_public_bind,
        allow_remote_deploy=server.allow_remote_deploy,
        auth_token_env=server.auth_token_env,
        auth_token_file=server.auth_token_file,
        rate_limit_requests=server.rate_limit_requests,
        rate_limit_window_seconds=server.rate_limit_window_seconds,
        project_root=settings.project_root,
        audit_security_event=record_security_event,
    )
    prepared_http = await http_server.prepare_http_server(
        dep_factory.make_http_deps(app),
        runtime=runtime,
        status_deps=dep_factory.make_status_deps(app),
        github_webhook_routes=github_webhook_routes(github_options.webhook_routes),
    )
    app.set_http_runner(prepared_http.runner)
    await http_server.activate_http_server(prepared_http)
    return prepared_http


def _start_ipc_watcher(app: PynchyApp) -> None:
    register_builtin_handlers()
    watcher = start_ipc_watcher(
        dep_factory.make_ipc_deps(app),
        ipc_base_dir=get_settings().data_dir / "ipc",
    )
    app.subsystem_tasks.add(create_background_task(watcher, name="ipc-watcher"))


async def _start_temporal_scheduler(app: PynchyApp) -> None:
    """Start Temporal polling and wait until its worker owns the task queue."""
    scheduler_deps = dep_factory.make_scheduler_deps(app)
    ready = asyncio.get_running_loop().create_future()
    app.subsystem_tasks.add(
        create_background_task(
            task_scheduler.start_scheduler_loop(scheduler_deps, ready=ready),
            name="scheduler",
        )
    )
    await ready


async def _activate_runtime_owners(
    app: PynchyApp,
    prepared_http: http_server.PreparedHttpServer,
) -> None:
    """Restore durable routes and start critical pollers behind their gates."""
    app.subsystem_tasks.add(create_background_task(gateway_manager.supervise_gateway()))
    await _start_temporal_scheduler(app)
    await app.start_linear_work_item_reconciliation()
    await http_server.recover_http_routes(prepared_http)
    await start_connection_runtimes(app)


async def _publish_runtime_owners(
    app: PynchyApp,
    prepared_http: http_server.PreparedHttpServer,
    interrupted_recovery: startup_handler.InterruptedTurnRecovery,
) -> None:
    """Commit startup success, then publish request and activity surfaces."""
    # Deployment success follows every critical owner readiness handshake.
    # HTTP publication and IPC task registration below are synchronous gate
    # releases, not additional readiness handshakes.
    await startup_handler.resolve_deploy_startup(
        interrupted_recovery,
        active_revision=current_deploy_revision(),
    )
    try:
        await startup_handler.finalize_deploy_startup(interrupted_recovery)
    except OSError:
        # A retained claimed continuation is safe and retryable on the next
        # startup; it must not invalidate an otherwise healthy deployment.
        logger.exception("Deploy continuation cleanup deferred")
    http_server.publish_http_server(prepared_http)
    _start_ipc_watcher(app)
    app.startup_readiness.mark_ready()


async def _start_runtime_owners(
    app: PynchyApp,
    interrupted_recovery: startup_handler.InterruptedTurnRecovery,
) -> None:
    """Start preflighted runtime owners in dependency order."""
    prepared_http = await _prepare_and_bind_control_plane(app)
    await _activate_runtime_owners(app, prepared_http)
    await _publish_runtime_owners(app, prepared_http, interrupted_recovery)


async def _prepare_state_and_subsystems(
    app: PynchyApp,
    continuation_path: Path,
) -> startup_handler.InterruptedTurnRecovery:
    """Start stateful runtime owners inside the deploy rollback boundary."""
    try:
        await _reconcile_state(app)
        interrupted_recovery = await startup_handler.prepare_interrupted_turn_recovery(
            app,
            continuation_path=continuation_path,
        )
        await _start_runtime_owners(app, interrupted_recovery)
    except BaseException as exc:  # rollback must also release waiters during cancellation.
        app.startup_readiness.mark_failed(exc)
        await app.subsystem_tasks.stop()
        try:
            await app.cleanup_http_runner()
        except Exception:  # noqa: BLE001 - preserve the startup error that triggers rollback.
            logger.exception("HTTP cleanup failed during startup rollback")
        try:
            await app.connection_runtime_owner.close()
        except Exception:  # noqa: BLE001 - preserve the startup error that triggers rollback.
            logger.exception("Connection runtime cleanup failed during startup rollback")
        if isinstance(exc, Exception) and await asyncio.to_thread(continuation_path.exists):
            await startup_handler.auto_rollback(continuation_path, exc)
        raise
    return interrupted_recovery


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
    continuation_path = startup_handler.claim_deploy_continuation(s.data_dir)

    try:
        await _initialize_core(app)
    except Exception as exc:  # startup rollback boundary; any init failure should trigger rollback.
        if await asyncio.to_thread(continuation_path.exists):
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
    # startup rollback boundary; any channel setup failure should trigger rollback.
    except Exception as exc:
        if await asyncio.to_thread(continuation_path.exists):
            await startup_handler.auto_rollback(continuation_path, exc)
        raise

    if not app.workspaces:
        default_channel = resolve_default_channel(
            app.channels, get_settings().command_center.connection
        )
        await startup_handler.setup_admin_group(app, default_channel)

    interrupted_recovery = await _prepare_state_and_subsystems(app, continuation_path)

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
