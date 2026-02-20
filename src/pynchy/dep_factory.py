"""Dependency adapter factories — compose subsystem dependencies from app state.

Extracted from app.py to keep the orchestrator focused on wiring.
These factory functions are called once during app startup to build
the composite dependency objects that subsystems require.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pynchy.adapters import (
    EventBusAdapter,
    GroupMetadataManager,
    GroupRegistrationManager,
    GroupRegistry,
    HostMessageBroadcaster,
    MessageBroadcaster,
    PeriodicAgentManager,
    QueueManager,
    SessionManager,
    UserMessageHandler,
)
from pynchy.config import get_settings
from pynchy.container_runner import write_groups_snapshot as _write_groups_snapshot
from pynchy.git_ops.utils import get_head_sha

if TYPE_CHECKING:
    from pynchy.app import PynchyApp
    from pynchy.git_ops.sync import GitSyncDeps
    from pynchy.http_server import HttpDeps
    from pynchy.ipc import IpcDeps
    from pynchy.status import StatusDeps
    from pynchy.task_scheduler import SchedulerDependencies


def _get_broadcasters(app: PynchyApp) -> tuple[MessageBroadcaster, HostMessageBroadcaster]:
    """Return the app's shared broadcaster pair.

    All subsystems reuse the same MessageBroadcaster and HostMessageBroadcaster
    instances from PynchyApp, ensuring a single code path for all channel sends.
    """
    return app._broadcaster, app._host_broadcaster


def make_scheduler_deps(app: PynchyApp) -> SchedulerDependencies:
    """Create the dependency object for the task scheduler."""
    group_registry = GroupRegistry(app.workspaces)
    queue_manager = QueueManager(app.queue)

    class SchedulerDeps:
        workspaces = group_registry.workspaces
        queue = queue_manager.queue
        broadcast_to_channels = app._broadcaster._broadcast_formatted

        @staticmethod
        async def run_agent(*args: Any, **kwargs: Any) -> str:
            return await app.run_agent(*args, **kwargs)

        @staticmethod
        async def handle_streamed_output(*args: Any, **kwargs: Any) -> bool:
            return await app.handle_streamed_output(*args, **kwargs)

    return SchedulerDeps()


async def _rebuild_and_deploy(
    *,
    host_broadcaster: HostMessageBroadcaster,
    group_registry: GroupRegistry,
    session_manager: SessionManager,
    previous_sha: str,
    rebuild: bool = True,
) -> None:
    """Shared rebuild + deploy logic used by IPC and git-sync paths.

    Optionally rebuilds the container image, then calls ``finalize_deploy``
    with all active sessions so every group gets resume continuity.
    """
    from pynchy.deploy import finalize_deploy

    chat_jid = group_registry.admin_chat_jid()
    if chat_jid:
        msg = (
            "Container files changed — rebuilding and restarting..."
            if rebuild
            else "Code/config changed — restarting..."
        )
        await host_broadcaster.broadcast_host_message(chat_jid, msg)

    if rebuild:
        from pynchy.deploy import build_container_image

        build_container_image()

    active_sessions = session_manager.get_active_sessions(group_registry.workspaces())

    await finalize_deploy(
        broadcast_host_message=host_broadcaster.broadcast_host_message,
        chat_jid=chat_jid,
        commit_sha=get_head_sha(),
        previous_sha=previous_sha,
        active_sessions=active_sessions,
    )


def make_http_deps(app: PynchyApp) -> HttpDeps:
    """Create the dependency object for the HTTP server."""
    _broadcaster, host_broadcaster = _get_broadcasters(app)
    group_registry = GroupRegistry(app.workspaces)
    session_manager = SessionManager(app.sessions, app._session_cleared)
    metadata_manager = GroupMetadataManager(app.workspaces, app.channels, app.get_available_groups)
    periodic_agent_manager = PeriodicAgentManager(app.workspaces)
    user_message_handler = UserMessageHandler(
        app._ingest_user_message, app.queue.enqueue_message_check
    )
    event_adapter = EventBusAdapter(app.event_bus)

    class HttpDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        admin_chat_jid = group_registry.admin_chat_jid
        channels_connected = metadata_manager.channels_connected
        get_groups = metadata_manager.get_groups
        get_messages = user_message_handler.get_messages
        send_user_message = user_message_handler.send_user_message
        get_periodic_agents = periodic_agent_manager.get_periodic_agents
        subscribe_events = event_adapter.subscribe_events

        def is_shutting_down(self) -> bool:
            return app._shutting_down

        def get_active_sessions(self) -> dict[str, str]:
            return session_manager.get_active_sessions(group_registry.workspaces())

    return HttpDeps()


def make_ipc_deps(app: PynchyApp) -> IpcDeps:
    """Create the dependency object for the IPC watcher."""
    broadcaster, host_broadcaster = _get_broadcasters(app)
    registration_manager = GroupRegistrationManager(
        app.workspaces, app._register_workspace, app._send_clear_confirmation
    )
    session_manager = SessionManager(app.sessions, app._session_cleared)
    metadata_manager = GroupMetadataManager(app.workspaces, app.channels, app.get_available_groups)
    queue_manager = QueueManager(app.queue)
    group_registry = GroupRegistry(app.workspaces)

    class IpcDeps:
        broadcast_to_channels = broadcaster._broadcast_to_channels
        broadcast_host_message = host_broadcaster.broadcast_host_message
        broadcast_system_notice = host_broadcaster.broadcast_system_notice
        workspaces = registration_manager.workspaces
        register_workspace = registration_manager.register_workspace
        sync_group_metadata = metadata_manager.sync_group_metadata
        get_available_groups = metadata_manager.get_available_groups
        write_groups_snapshot = staticmethod(_write_groups_snapshot)
        has_active_session = session_manager.has_active_session
        clear_session = session_manager.clear_session
        clear_chat_history = registration_manager.clear_chat_history
        enqueue_message_check = queue_manager.enqueue_message_check
        channels = metadata_manager.channels

        def get_active_sessions(self) -> dict[str, str]:
            return session_manager.get_active_sessions(group_registry.workspaces())

        async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
            await _rebuild_and_deploy(
                host_broadcaster=host_broadcaster,
                group_registry=group_registry,
                session_manager=session_manager,
                previous_sha=previous_sha,
                rebuild=rebuild,
            )

    return IpcDeps()


def make_status_deps(app: PynchyApp) -> StatusDeps:
    """Create the dependency object for the status collector."""
    group_registry = GroupRegistry(app.workspaces)
    session_manager = SessionManager(app.sessions, app._session_cleared)
    metadata_manager = GroupMetadataManager(app.workspaces, app.channels, app.get_available_groups)

    class _StatusDeps:
        def is_shutting_down(self) -> bool:
            return app._shutting_down

        def get_channel_status(self) -> dict[str, bool]:
            return {ch.name: ch.is_connected() for ch in metadata_manager.channels()}

        def get_queue_snapshot(self) -> dict[str, Any]:
            q = app.queue
            per_group: dict[str, Any] = {}
            for jid, state in q._groups.items():
                # Resolve group folder for display
                ws = app.workspaces.get(jid)
                label = ws.folder if ws else jid
                per_group[label] = {
                    "active": state.active,
                    "is_task": state.active_is_task,
                    "pending_messages": state.pending_messages,
                    "pending_tasks": len(state.pending_tasks),
                }
            return {
                "active_containers": q._active_count,
                "max_concurrent": get_settings().container.max_concurrent,
                "groups_waiting": len(q._waiting_groups),
                "per_group": per_group,
            }

        def get_gateway_info(self) -> dict[str, Any]:
            from pynchy.container_runner.gateway import LiteLLMGateway, get_gateway

            gw = get_gateway()
            if gw is None:
                return {"mode": "none"}
            mode = "litellm" if isinstance(gw, LiteLLMGateway) else "builtin"
            return {"mode": mode, "port": gw.port, "key": gw.key}

        def get_active_sessions_count(self) -> int:
            active = session_manager.get_active_sessions(group_registry.workspaces())
            return len(active)

        def get_workspace_count(self) -> int:
            return len(app.workspaces)

    return _StatusDeps()


def make_git_sync_deps(app: PynchyApp) -> GitSyncDeps:
    """Create the dependency object for the git sync loop."""
    _broadcaster, host_broadcaster = _get_broadcasters(app)
    group_registry = GroupRegistry(app.workspaces)
    session_manager = SessionManager(app.sessions, app._session_cleared)

    class GitSyncDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        broadcast_system_notice = host_broadcaster.broadcast_system_notice

        def has_active_session(self, group_folder: str) -> bool:
            return session_manager.has_active_session(group_folder)

        def workspaces(self) -> dict[str, Any]:
            return group_registry.workspaces()

        async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
            await _rebuild_and_deploy(
                host_broadcaster=host_broadcaster,
                group_registry=group_registry,
                session_manager=session_manager,
                previous_sha=previous_sha,
                rebuild=rebuild,
            )

    return GitSyncDeps()
