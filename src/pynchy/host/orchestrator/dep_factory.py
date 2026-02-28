"""Dependency adapter factories — compose subsystem dependencies from app state.

Extracted from app.py to keep the orchestrator focused on wiring.
These factory functions are called once during app startup to build
the composite dependency objects that subsystems require.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pynchy.config import get_settings
from pynchy.host.container_manager import write_groups_snapshot as _write_groups_snapshot
from pynchy.host.git_ops.utils import get_head_sha
from pynchy.host.orchestrator.adapters import (
    EventBusAdapter,
    GroupMetadataManager,
    GroupRegistrationManager,
    HostMessageBroadcaster,
    MessageBroadcaster,
    PeriodicAgentManager,
    SessionManager,
    UserMessageHandler,
    find_admin_jid,
)

if TYPE_CHECKING:
    from pynchy.host.container_manager.ipc import IpcDeps
    from pynchy.host.git_ops.sync import GitSyncDeps
    from pynchy.host.orchestrator.app import PynchyApp
    from pynchy.host.orchestrator.http_server import HttpDeps
    from pynchy.host.orchestrator.status import StatusDeps
    from pynchy.host.orchestrator.task_scheduler import SchedulerDependencies


def _get_broadcasters(app: PynchyApp) -> tuple[MessageBroadcaster, HostMessageBroadcaster]:
    """Return the app's shared broadcaster pair.

    All subsystems reuse the same MessageBroadcaster and HostMessageBroadcaster
    instances from PynchyApp, ensuring a single code path for all channel sends.
    """
    return app._broadcaster, app._host_broadcaster


def make_scheduler_deps(app: PynchyApp) -> SchedulerDependencies:
    """Create the dependency object for the task scheduler."""

    class SchedulerDeps:
        broadcast_to_channels = app._broadcaster._broadcast_formatted

        def workspaces(self) -> dict[str, Any]:
            return app.workspaces

        @property
        def queue(self) -> Any:
            return app.queue

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
    workspaces: dict[str, Any],
    session_manager: SessionManager,
    previous_sha: str,
    rebuild: bool = True,
) -> None:
    """Shared rebuild + deploy logic used by IPC and git-sync paths.

    Optionally rebuilds the container image, then calls ``finalize_deploy``
    with all active sessions so every group gets resume continuity.
    """
    from pynchy.host.orchestrator.deploy import finalize_deploy

    chat_jid = find_admin_jid(workspaces)
    if chat_jid:
        msg = (
            "Container files changed — rebuilding and restarting..."
            if rebuild
            else "Code/config changed — restarting..."
        )
        await host_broadcaster.broadcast_host_message(chat_jid, msg)

    if rebuild:
        from pynchy.host.orchestrator.deploy import build_container_image

        await asyncio.to_thread(build_container_image)

    active_sessions = session_manager.get_active_sessions(workspaces)

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
    session_manager = SessionManager(app.sessions, app._session_cleared)
    metadata_manager = GroupMetadataManager(app.workspaces, app.channels, app.get_available_groups)
    periodic_agent_manager = PeriodicAgentManager(app.workspaces)
    user_message_handler = UserMessageHandler(
        app._ingest_user_message, app.queue.enqueue_message_check
    )
    event_adapter = EventBusAdapter(app.event_bus)

    class HttpDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        channels_connected = metadata_manager.channels_connected
        get_groups = metadata_manager.get_groups
        get_messages = user_message_handler.get_messages
        send_user_message = user_message_handler.send_user_message
        get_periodic_agents = periodic_agent_manager.get_periodic_agents
        subscribe_events = event_adapter.subscribe_events

        def admin_chat_jid(self) -> str:
            return find_admin_jid(app.workspaces)

        def is_shutting_down(self) -> bool:
            return app._shutting_down

        def get_active_sessions(self) -> dict[str, str]:
            return session_manager.get_active_sessions(app.workspaces)

    return HttpDeps()


def make_ipc_deps(app: PynchyApp) -> IpcDeps:
    """Create the dependency object for the IPC watcher."""
    broadcaster, host_broadcaster = _get_broadcasters(app)
    registration_manager = GroupRegistrationManager(
        app.workspaces, app._register_workspace, app._send_clear_confirmation
    )
    session_manager = SessionManager(app.sessions, app._session_cleared)
    metadata_manager = GroupMetadataManager(app.workspaces, app.channels, app.get_available_groups)

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
        channels = metadata_manager.channels

        def enqueue_message_check(self, group_jid: str) -> None:
            app.queue.enqueue_message_check(group_jid)

        def get_active_sessions(self) -> dict[str, str]:
            return session_manager.get_active_sessions(app.workspaces)

        async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
            await _rebuild_and_deploy(
                host_broadcaster=host_broadcaster,
                workspaces=app.workspaces,
                session_manager=session_manager,
                previous_sha=previous_sha,
                rebuild=rebuild,
            )

    return IpcDeps()


def make_status_deps(app: PynchyApp) -> StatusDeps:
    """Create the dependency object for the status collector."""
    session_manager = SessionManager(app.sessions, app._session_cleared)
    metadata_manager = GroupMetadataManager(app.workspaces, app.channels, app.get_available_groups)

    class _StatusDeps:
        def is_shutting_down(self) -> bool:
            return app._shutting_down

        def get_channel_status(self) -> dict[str, bool]:
            return {ch.name: ch.is_connected() for ch in metadata_manager.channels()}

        def get_queue_snapshot(self) -> dict[str, Any]:
            raw = app.queue.snapshot()
            meta = raw.pop("_meta", {})
            # Resolve JID → folder name for display
            per_group: dict[str, Any] = {}
            for jid, data in raw.items():
                ws = app.workspaces.get(jid)
                label = ws.folder if ws else jid
                per_group[label] = data
            return {
                "active_containers": meta.get("active_count", 0),
                "max_concurrent": get_settings().container.max_concurrent,
                "groups_waiting": meta.get("waiting_count", 0),
                "per_group": per_group,
            }

        def get_gateway_info(self) -> dict[str, Any]:
            from pynchy.host.container_manager.gateway import LiteLLMGateway, get_gateway

            gw = get_gateway()
            if gw is None:
                return {"mode": "none"}
            mode = "litellm" if isinstance(gw, LiteLLMGateway) else "builtin"
            return {"mode": mode, "port": gw.port, "key": gw.key}

        def get_active_sessions_count(self) -> int:
            active = session_manager.get_active_sessions(app.workspaces)
            return len(active)

        def get_workspace_count(self) -> int:
            return len(app.workspaces)

    return _StatusDeps()


def make_git_sync_deps(app: PynchyApp) -> GitSyncDeps:
    """Create the dependency object for the git sync loop."""
    _broadcaster, host_broadcaster = _get_broadcasters(app)
    session_manager = SessionManager(app.sessions, app._session_cleared)

    class GitSyncDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        broadcast_system_notice = host_broadcaster.broadcast_system_notice

        def has_active_session(self, group_folder: str) -> bool:
            return session_manager.has_active_session(group_folder)

        def workspaces(self) -> dict[str, Any]:
            return app.workspaces

        async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
            await _rebuild_and_deploy(
                host_broadcaster=host_broadcaster,
                workspaces=app.workspaces,
                session_manager=session_manager,
                previous_sha=previous_sha,
                rebuild=rebuild,
            )

    return GitSyncDeps()
