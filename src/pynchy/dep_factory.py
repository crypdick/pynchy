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
from pynchy.container_runner import write_groups_snapshot as _write_groups_snapshot
from pynchy.git_ops.utils import get_head_sha

if TYPE_CHECKING:
    from pynchy.app import PynchyApp
    from pynchy.git_ops.sync import GitSyncDeps
    from pynchy.ipc import IpcDeps
    from pynchy.runtime.http_server import HttpDeps
    from pynchy.task_scheduler import SchedulerDependencies


def _get_broadcasters(app: PynchyApp) -> tuple[MessageBroadcaster, HostMessageBroadcaster]:
    """Return the app's shared broadcaster pair.

    All subsystems reuse the same MessageBroadcaster and HostMessageBroadcaster
    instances from PynchyApp, ensuring a single code path for all channel sends.
    """
    return app._broadcaster, app._host_broadcaster


def make_scheduler_deps(app: PynchyApp) -> SchedulerDependencies:
    """Create the dependency object for the task scheduler."""
    group_registry = GroupRegistry(app.registered_groups)
    queue_manager = QueueManager(app.queue)

    class SchedulerDeps:
        registered_groups = group_registry.registered_groups
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

    chat_jid = group_registry.god_chat_jid()
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

    active_sessions = session_manager.get_active_sessions(group_registry.registered_groups())

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
    group_registry = GroupRegistry(app.registered_groups)
    session_manager = SessionManager(app.sessions, app._session_cleared)
    metadata_manager = GroupMetadataManager(
        app.registered_groups, app.channels, app.get_available_groups
    )
    periodic_agent_manager = PeriodicAgentManager(app.registered_groups)
    user_message_handler = UserMessageHandler(
        app._ingest_user_message, app.queue.enqueue_message_check
    )
    event_adapter = EventBusAdapter(app.event_bus)

    class HttpDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        god_chat_jid = group_registry.god_chat_jid
        channels_connected = metadata_manager.channels_connected
        get_groups = metadata_manager.get_groups
        get_messages = user_message_handler.get_messages
        send_user_message = user_message_handler.send_user_message
        get_periodic_agents = periodic_agent_manager.get_periodic_agents
        subscribe_events = event_adapter.subscribe_events

        def is_shutting_down(self) -> bool:
            return app._shutting_down

        def get_active_sessions(self) -> dict[str, str]:
            return session_manager.get_active_sessions(group_registry.registered_groups())

    return HttpDeps()


def make_ipc_deps(app: PynchyApp) -> IpcDeps:
    """Create the dependency object for the IPC watcher."""
    broadcaster, host_broadcaster = _get_broadcasters(app)
    registration_manager = GroupRegistrationManager(
        app.registered_groups, app._register_group, app._send_clear_confirmation
    )
    session_manager = SessionManager(app.sessions, app._session_cleared)
    metadata_manager = GroupMetadataManager(
        app.registered_groups, app.channels, app.get_available_groups
    )
    queue_manager = QueueManager(app.queue)
    group_registry = GroupRegistry(app.registered_groups)

    class IpcDeps:
        broadcast_to_channels = broadcaster._broadcast_to_channels
        broadcast_host_message = host_broadcaster.broadcast_host_message
        broadcast_system_notice = host_broadcaster.broadcast_system_notice
        registered_groups = registration_manager.registered_groups
        register_group = registration_manager.register_group
        sync_group_metadata = metadata_manager.sync_group_metadata
        get_available_groups = metadata_manager.get_available_groups
        write_groups_snapshot = staticmethod(_write_groups_snapshot)
        clear_session = session_manager.clear_session
        clear_chat_history = registration_manager.clear_chat_history
        enqueue_message_check = queue_manager.enqueue_message_check
        channels = metadata_manager.channels

        def get_active_sessions(self) -> dict[str, str]:
            return session_manager.get_active_sessions(group_registry.registered_groups())

        async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
            await _rebuild_and_deploy(
                host_broadcaster=host_broadcaster,
                group_registry=group_registry,
                session_manager=session_manager,
                previous_sha=previous_sha,
                rebuild=rebuild,
            )

    return IpcDeps()


def make_git_sync_deps(app: PynchyApp) -> GitSyncDeps:
    """Create the dependency object for the git sync loop."""
    _broadcaster, host_broadcaster = _get_broadcasters(app)
    group_registry = GroupRegistry(app.registered_groups)
    session_manager = SessionManager(app.sessions, app._session_cleared)

    class GitSyncDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        broadcast_system_notice = host_broadcaster.broadcast_system_notice

        def registered_groups(self) -> dict[str, Any]:
            return group_registry.registered_groups()

        async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
            await _rebuild_and_deploy(
                host_broadcaster=host_broadcaster,
                group_registry=group_registry,
                session_manager=session_manager,
                previous_sha=previous_sha,
                rebuild=rebuild,
            )

    return GitSyncDeps()
