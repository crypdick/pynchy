"""Dependency adapter factories — compose subsystem dependencies from app state.

Extracted from app.py to keep the orchestrator focused on wiring.
These factory functions are called once during app startup to build
the composite dependency objects that subsystems require.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pynchy.host.container_manager.gateway as gateway_manager
from pynchy.config import get_settings
from pynchy.conversation.models import (  # noqa: TC001, RUF100 - beartype resolves helper annotations at runtime.
    Conversation,
    ConversationId,
    ExternalDeliveryIdentity,
    TerminalConversationRetirement,
)
from pynchy.host.container_manager import write_groups_snapshot as _write_groups_snapshot
from pynchy.host.container_manager.ipc import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    IpcDeps,
)
from pynchy.host.container_manager.session import destroy_session
from pynchy.host.git_ops.sync import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    GitSyncDeps,
)
from pynchy.host.git_ops.sync_poll import get_deploy_config_hash
from pynchy.host.git_ops.utils import get_head_sha
from pynchy.host.orchestrator import session_handler
from pynchy.host.orchestrator.adapters import (
    GroupMetadataManager,
    GroupRegistrationManager,
    HostMessageBroadcaster,
    MessageBroadcaster,
    SessionManager,
    resolve_admin_notification_jid,
)
from pynchy.host.orchestrator.app import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    PynchyApp,
)
from pynchy.host.orchestrator.http_server import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    HttpServerDeps,
)
from pynchy.host.orchestrator.status import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    StatusDeps,
)
from pynchy.host.orchestrator.task_scheduler import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    SchedulerDependencies,
)
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.host.orchestrator.temporal.scheduler import (
    start_deploy_workflow,
    start_scheduled_agent_task_workflow,
)
from pynchy.host.orchestrator.temporal.schedules import agent_task_workflow_id
from pynchy.host.orchestrator.temporal.workflow_control import cancel_scheduled_agent_workflow
from pynchy.plugins.speech import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    SpeechSynthesizer,
)
from pynchy.state import (
    cancel_task_and_checkpoint,
    clear_session,
    complete_conversation_delivery,
    conversation_control_state_matches,
    get_active_work_item_execution,
    get_conversation,
    get_conversation_control_binding,
    get_task_by_id,
    get_tasks_for_conversation,
    get_terminal_conversation_retirement,
    get_work_item_execution_for_task,
)
from pynchy.state import (
    retire_conversation_for_terminal as _retire_terminal_conversation_state,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves dependency adapter annotations at runtime.
    Channel,
    GroupFolder,
    NewMessage,
    RuntimeId,
    ScheduledTask,
    SessionId,
    WorkItemExecution,
    WorkspaceProfile,
)
from pynchy.utils import create_background_task


def _get_broadcasters(app: PynchyApp) -> tuple[MessageBroadcaster, HostMessageBroadcaster]:
    """Return the app's shared broadcaster pair.

    All subsystems reuse the same MessageBroadcaster and HostMessageBroadcaster
    instances from PynchyApp, ensuring a single code path for all channel sends.
    """
    return app.message_broadcaster, app.host_broadcaster


def _schedule_interactive_turn(app: PynchyApp, chat_jid: str) -> None:
    create_background_task(
        app.start_interactive_turn(chat_jid),
        name=f"interactive-turn-{chat_jid[:20]}",
    )


async def _active_linear_execution_for_conversation(
    conversation: Conversation,
) -> WorkItemExecution | None:
    """Return a live managed execution only for its matching Linear issue."""
    namespace = str(conversation.subject.namespace)
    if not namespace.startswith("linear:") or not namespace.endswith(":issue"):
        return None
    execution = await get_active_work_item_execution(str(conversation.subject.key))
    if execution is None or execution.workspace != str(conversation.workspace):
        return None
    return execution


async def _task_owned_by_execution(
    tasks: list[ScheduledTask],
    execution: WorkItemExecution | None,
) -> list[ScheduledTask]:
    """Recover active task ownership recorded before conversation binding existed."""
    if execution is None or execution.task_id is None:
        return tasks
    if any(task.id == execution.task_id for task in tasks):
        return tasks
    task = await get_task_by_id(execution.task_id)
    if task is None or task.status not in {"active", "paused"}:
        return tasks
    return [*tasks, task]


async def _retire_conversation_tasks(conversation_id: ConversationId) -> None:
    """Cancel active workflows before retiring their durable task checkpoints."""
    conversation = await get_conversation(conversation_id)
    if conversation is None:
        raise RuntimeError(f"Terminal task retirement lost conversation: {conversation_id}")
    execution = await _active_linear_execution_for_conversation(conversation)
    tasks = await _task_owned_by_execution(
        await get_tasks_for_conversation(str(conversation_id)),
        execution,
    )
    workflow_ids = {agent_task_workflow_id(task) for task in tasks if task.schedule_type == "once"}
    for task in tasks:
        task_execution = await get_work_item_execution_for_task(task.id)
        if (
            task_execution is not None
            and task_execution.status.is_active
            and task_execution.temporal_workflow_id is not None
        ):
            workflow_ids.add(task_execution.temporal_workflow_id)
    if execution is not None and execution.temporal_workflow_id is not None:
        workflow_ids.add(execution.temporal_workflow_id)
    for workflow_id in sorted(workflow_ids):
        await cancel_scheduled_agent_workflow(workflow_id)
    for task in tasks:
        await cancel_task_and_checkpoint(task.id)


async def _retire_terminal_conversation(
    conversation_id: ConversationId,
    *,
    preserve_delivery: ExternalDeliveryIdentity,
    control_state_revision: str | None,
) -> TerminalConversationRetirement:
    """Adapt the atomic state retirement transaction for webhook runtime cleanup."""
    return await _retire_terminal_conversation_state(
        conversation_id,
        preserve_delivery=preserve_delivery,
        control_state_revision=control_state_revision,
    )


def make_scheduler_deps(app: PynchyApp) -> SchedulerDependencies:
    """Return the app as the shared dependency object for Temporal activities.

    Temporal's scheduler worker also runs interactive, deploy, reconciliation,
    and git-sync activities. Those activities deliberately share the app's live
    workspace map and lifecycle state instead of receiving a scheduler-only
    facade that can drift from the broader runtime contract.
    """
    return cast("SchedulerDependencies", app)


async def _start_temporal_deploy(
    *,
    host_broadcaster: HostMessageBroadcaster,
    workspaces: dict[str, WorkspaceProfile],
    previous_sha: str,
    rebuild: bool = True,
) -> None:
    """Start a Temporal deploy workflow; in-flight turns are checkpointed in SQLite."""
    chat_jid = resolve_admin_notification_jid(
        workspaces, get_settings().notifications.admin_workspace
    )
    if chat_jid:
        msg = (
            "Container files changed — starting deploy workflow..."
            if rebuild
            else "Code/config changed — starting deploy workflow..."
        )
        await host_broadcaster.broadcast_host_message(chat_jid, msg)

    await start_deploy_workflow(
        DeployRequest(
            chat_jid=chat_jid,
            commit_sha=get_head_sha(),
            config_hash=get_deploy_config_hash(),
            previous_sha=previous_sha,
            rebuild=rebuild,
            reason="dependency_adapter",
        )
    )


def make_http_deps(app: PynchyApp) -> HttpServerDeps:
    """Create the dependency object for the HTTP server."""
    _broadcaster, host_broadcaster = _get_broadcasters(app)

    class HttpDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        complete_conversation_delivery = staticmethod(complete_conversation_delivery)
        conversation_control_state_matches = staticmethod(conversation_control_state_matches)
        get_conversation = staticmethod(get_conversation)
        get_conversation_control_binding = staticmethod(get_conversation_control_binding)
        get_terminal_conversation_retirement = staticmethod(get_terminal_conversation_retirement)

        async def ingest_runtime_harness_message(self, jid: str, content: str) -> None:
            await app.on_inbound(
                jid,
                NewMessage(
                    id=f"runtime-{uuid4()}",
                    chat_jid=jid,
                    sender="runtime-user",
                    sender_name="Runtime User",
                    content=content,
                    timestamp=datetime.now(UTC).isoformat(),
                    is_from_me=False,
                    metadata={"source": "runtime_harness"},
                ),
            )

        def admin_chat_jid(self) -> str:
            return resolve_admin_notification_jid(
                app.workspaces, get_settings().notifications.admin_workspace
            )

        def get_plugin_manager(self) -> object:
            if app.plugin_manager is None:
                raise RuntimeError("Plugin manager is unavailable during HTTP startup")
            return app.plugin_manager

        def get_workspace(self, folder: str) -> WorkspaceProfile | None:
            return next(
                (workspace for workspace in app.workspaces.values() if workspace.folder == folder),
                None,
            )

        def dispatch_scheduled_task(self, task: ScheduledTask) -> None:
            create_background_task(
                start_scheduled_agent_task_workflow(task),
                name=f"webhook-task-{task.id[-36:]}",
            )

        def channels(self) -> list[Channel]:
            return app.channels

        def workspaces(self) -> dict[str, WorkspaceProfile]:
            return app.workspaces

        async def register_workspace(self, profile: WorkspaceProfile) -> None:
            await app.register_workspace(profile)

        async def unregister_workspace(self, jid: str) -> None:
            await app.unregister_workspace(jid)

        async def rebind_workspace(self, profile: WorkspaceProfile) -> None:
            await app.rebind_workspace(profile)

        async def bind_session(self, folder: str, session_id: SessionId) -> None:
            await app.bind_routed_session(folder, session_id)

        async def retire_conversation_runtime(self, folder: GroupFolder) -> None:
            """Stop local-only routed work without invoking Linear reset behavior."""
            runtime_id = RuntimeId(folder)
            app.queue.clear_pending_tasks(runtime_id)
            app.queue.clear_pending_messages(runtime_id)
            await app.queue.stop_active_process_for_control(runtime_id)
            app.queue.clear_pending_messages(runtime_id)
            await destroy_session(folder)
            app.sessions.pop(folder, None)
            app.session_cleared.add(folder)
            await clear_session(folder)

        retire_conversation_tasks = staticmethod(_retire_conversation_tasks)
        retire_conversation_for_terminal = staticmethod(_retire_terminal_conversation)

        async def ingest_message(self, jid: str, message: NewMessage) -> None:
            await app.on_inbound(jid, message)

    return HttpDeps()


def make_ipc_deps(app: PynchyApp) -> IpcDeps:
    """Create the dependency object for the IPC watcher."""
    broadcaster, host_broadcaster = _get_broadcasters(app)
    registration_manager = GroupRegistrationManager(
        app.workspaces, app.register_workspace, app.send_clear_confirmation
    )
    session_manager = SessionManager(app.sessions, app.session_cleared)
    metadata_manager = GroupMetadataManager(app.channels, app.get_available_groups)

    class IpcDeps:
        broadcast_to_channels = broadcaster.broadcast_to_channels
        broadcast_host_message = host_broadcaster.broadcast_host_message
        broadcast_system_notice = host_broadcaster.broadcast_system_notice
        workspaces = registration_manager.workspaces
        register_workspace = registration_manager.register_workspace
        sync_group_metadata = metadata_manager.sync_group_metadata
        get_available_groups = metadata_manager.get_available_groups
        write_groups_snapshot = staticmethod(_write_groups_snapshot)
        has_active_session = session_manager.has_active_session
        clear_chat_history = registration_manager.clear_chat_history
        channels = metadata_manager.channels

        async def clear_session(self, group_folder: str) -> None:
            group = next(
                (
                    workspace
                    for workspace in app.workspaces.values()
                    if workspace.folder == group_folder
                ),
                None,
            )
            if group is None:
                raise RuntimeError(f"Context-reset runtime no longer exists: {group_folder}")
            await session_handler.clear_durable_context(app, group)

        def enqueue_message_check(self, group_jid: str) -> None:
            _schedule_interactive_turn(app, group_jid)

        def get_active_sessions(self) -> dict[str, str]:
            return session_manager.get_active_sessions(app.workspaces)

        def connection_statuses(self) -> dict[str, bool]:
            return app.connection_runtime_owner.status()

        async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
            await _start_temporal_deploy(
                host_broadcaster=host_broadcaster,
                workspaces=app.workspaces,
                previous_sha=previous_sha,
                rebuild=rebuild,
            )

    return IpcDeps()


def make_status_deps(app: PynchyApp) -> StatusDeps:
    """Create the dependency object for the status collector."""
    session_manager = SessionManager(app.sessions, app.session_cleared)
    metadata_manager = GroupMetadataManager(app.channels, app.get_available_groups)

    class _StatusDeps:
        def is_shutting_down(self) -> bool:
            return app.is_shutting_down()

        def get_channel_status(self) -> dict[str, bool]:
            return {ch.name: ch.is_connected() for ch in metadata_manager.channels()}

        def get_connection_status(self) -> dict[str, bool]:
            return app.connection_runtime_owner.status()

        def get_queue_snapshot(self) -> dict[str, Any]:
            raw = app.queue.snapshot()
            meta = raw.pop("_meta", {})
            return {
                "active_containers": meta.get("active_count", 0),
                "max_concurrent": get_settings().container.max_concurrent,
                "groups_waiting": meta.get("waiting_count", 0),
                "per_group": raw,
            }

        def get_gateway_info(self) -> dict[str, Any]:
            gw = gateway_manager.get_gateway()
            if gw is None:
                return {"mode": "none"}
            mode = "litellm" if isinstance(gw, gateway_manager.LiteLLMGateway) else "builtin"
            return {
                "mode": mode,
                "port": gw.port,
                "key": gw.key,
                "redaction": gw.redaction_posture.value,
            }

        def get_active_sessions_count(self) -> int:
            active = session_manager.get_active_sessions(app.workspaces)
            return len(active)

        def get_workspace_count(self) -> int:
            return len(app.workspaces)

        def get_speech_synthesizer(self) -> SpeechSynthesizer | None:
            return app.get_speech_synthesizer()

    return _StatusDeps()


def make_git_sync_deps(app: PynchyApp) -> GitSyncDeps:
    """Create the dependency object for the git sync loop."""
    _broadcaster, host_broadcaster = _get_broadcasters(app)
    session_manager = SessionManager(app.sessions, app.session_cleared)

    class GitSyncDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        broadcast_system_notice = host_broadcaster.broadcast_system_notice

        def has_active_session(self, group_folder: str) -> bool:
            return session_manager.has_active_session(group_folder)

        def workspaces(self) -> dict[str, Any]:
            return app.workspaces

        async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
            await _start_temporal_deploy(
                host_broadcaster=host_broadcaster,
                workspaces=app.workspaces,
                previous_sha=previous_sha,
                rebuild=rebuild,
            )

    return GitSyncDeps()
