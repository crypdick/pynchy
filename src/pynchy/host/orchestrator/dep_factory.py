"""Dependency adapter factories compose subsystem dependencies from app state."""

from __future__ import annotations

# allow: file-length - local IPC composition binds the startup-owned snapshot data directory.
from collections.abc import (
    Sequence,  # noqa: TC003, RUF100 - beartype resolves channel collections at runtime.
)
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

import pynchy.host.container_manager.gateway as gateway_manager
from pynchy.config import get_settings
from pynchy.config.jobs import JobConfig
from pynchy.config.models import WorkspaceConfig
from pynchy.host.container_manager import write_groups_snapshot as _write_groups_snapshot
from pynchy.host.container_manager.ipc import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    IpcDeps,
)
from pynchy.host.container_manager.ipc.protocol import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    CreatePeriodicAgentRequest,
)
from pynchy.host.container_manager.session import destroy_session
from pynchy.host.git_ops.sync import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    GitSyncDeps,
)
from pynchy.host.git_ops.sync_poll import get_deploy_config_hash
from pynchy.host.git_ops.utils import get_head_sha
from pynchy.host.orchestrator import session_handler, workspace_config
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
from pynchy.host.orchestrator.messaging import pending_questions
from pynchy.host.orchestrator.scheduled_work_status import collect_scheduled_work
from pynchy.host.orchestrator.source_health_deps import SourceHealthProjection
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
from pynchy.host.orchestrator.temporal.status import get_temporal_orchestration_states
from pynchy.host.orchestrator.terminal_task_retirement import retire_conversation_tasks
from pynchy.logger import logger
from pynchy.plugins.speech import (  # noqa: TC001, RUF100 - beartype resolves dependency factory annotations at runtime.
    SpeechSynthesizer,
)
from pynchy.state import (
    clear_session,
    complete_conversation_delivery,
    conversation_control_state_matches,
    create_host_job,
    create_task,
    delete_host_job,
    delete_task,
    get_all_host_jobs,
    get_all_tasks,
    get_conversation,
    get_conversation_control_binding,
    get_host_job_by_id,
    get_task_by_id,
    get_task_run_logs,
    get_terminal_conversation_retirement,
    resume_task,
    update_host_job,
    update_task,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves dependency adapter annotations at runtime.
    Channel,
    GroupFolder,
    HostJob,
    NewMessage,
    RuntimeId,
    ScheduledTask,
    SessionId,
    SessionPolicy,
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


@runtime_checkable
class _GroupCreationChannel(Protocol):
    name: str

    async def create_group(self, name: str) -> str | None: ...


def _command_center_channel(
    channels: Sequence[object], command_center: str
) -> _GroupCreationChannel | None:
    return next(
        (
            cast("_GroupCreationChannel", channel)
            for channel in channels
            if getattr(channel, "name", None) == command_center and hasattr(channel, "create_group")
        ),
        None,
    )


def _valid_jid(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class _PendingQuestionStore:
    """Adapter for the application-owned pending-question persistence."""

    create = staticmethod(pending_questions.create_pending_question)
    update_message_id = staticmethod(pending_questions.update_message_id)
    resolve = staticmethod(pending_questions.resolve_pending_question)


class _ScheduledWorkStore:
    """Adapter for the application-owned scheduled-work persistence."""

    create_task = staticmethod(create_task)
    create_host_job = staticmethod(create_host_job)
    get_task_by_id = staticmethod(get_task_by_id)
    get_host_job_by_id = staticmethod(get_host_job_by_id)
    update_task = staticmethod(update_task)
    update_host_job = staticmethod(update_host_job)
    resume_task = staticmethod(resume_task)
    delete_task = staticmethod(delete_task)
    delete_host_job = staticmethod(delete_host_job)


async def _scheduled_work_status(
    source_group: str,
    *,
    is_admin: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    async def visible_tasks() -> list[ScheduledTask]:
        tasks = await get_all_tasks()
        return tasks if is_admin else [task for task in tasks if task.group_folder == source_group]

    async def visible_host_jobs() -> list[HostJob]:
        return await get_all_host_jobs() if is_admin else []

    scheduler = get_settings().scheduler
    return await collect_scheduled_work(
        visible_tasks,
        visible_host_jobs,
        lambda task_id: get_task_run_logs(task_id, limit=5),
        get_temporal_orchestration_states,
        (scheduler.temporal_address, scheduler.temporal_namespace),
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


async def _request_ipc_deploy(
    *,
    workspaces: dict[str, WorkspaceProfile],
    chat_jid: str | None,
    commit_sha: str,
    rebuild: bool,
    resume_prompt: str,
) -> None:
    """Start an agent-requested deploy through the configured notification target."""
    target_jid = chat_jid or resolve_admin_notification_jid(
        workspaces, get_settings().notifications.admin_workspace
    )
    if not target_jid:
        logger.error("Deploy request missing chatJid and no notification target resolved")
        return
    await start_deploy_workflow(
        DeployRequest(
            chat_jid=target_jid,
            commit_sha=commit_sha,
            config_hash=get_deploy_config_hash(),
            previous_sha=commit_sha,
            resume_prompt=resume_prompt,
            rebuild=rebuild,
            reason="ipc",
        )
    )


def make_http_deps(app: PynchyApp) -> HttpServerDeps:
    """Create the dependency object for the HTTP server."""
    _broadcaster, host_broadcaster = _get_broadcasters(app)
    settings = get_settings()

    class HttpDeps:
        broadcast_host_message = host_broadcaster.broadcast_host_message
        complete_conversation_delivery = staticmethod(complete_conversation_delivery)
        conversation_control_state_matches = staticmethod(conversation_control_state_matches)
        get_conversation = staticmethod(get_conversation)
        get_conversation_control_binding = staticmethod(get_conversation_control_binding)
        get_terminal_conversation_retirement = staticmethod(get_terminal_conversation_retirement)
        data_dir = settings.data_dir
        project_root = settings.project_root

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

        retire_conversation_tasks = staticmethod(retire_conversation_tasks)

        async def ingest_message(self, jid: str, message: NewMessage) -> None:
            await app.on_inbound(jid, message)

    return HttpDeps()


def make_ipc_deps(app: PynchyApp) -> IpcDeps:
    """Create the dependency object for the IPC watcher."""
    snapshot_data_dir = get_settings().data_dir
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

        def write_groups_snapshot(
            self,
            group_folder: str,
            available_groups: list[Any],
            registered_jids: set[str],
            *,
            is_admin: bool,
        ) -> None:
            _write_groups_snapshot(
                snapshot_data_dir,
                group_folder,
                available_groups,
                registered_jids,
                is_admin=is_admin,
            )

        has_active_session = session_manager.has_active_session
        clear_chat_history = registration_manager.clear_chat_history
        channels = metadata_manager.channels
        pending_question_store = staticmethod(_PendingQuestionStore)
        scheduled_work_store = staticmethod(_ScheduledWorkStore)
        messaging_source_health = staticmethod(SourceHealthProjection)
        default_agent_name = staticmethod(lambda: get_settings().agent.name)

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

        async def request_deploy(
            self,
            *,
            chat_jid: str | None,
            commit_sha: str,
            rebuild: bool,
            resume_prompt: str,
        ) -> None:
            await _request_ipc_deploy(
                workspaces=app.workspaces,
                chat_jid=chat_jid,
                commit_sha=commit_sha,
                rebuild=rebuild,
                resume_prompt=resume_prompt,
            )

        async def create_periodic_agent(self, request: CreatePeriodicAgentRequest) -> None:
            settings = get_settings()
            group_dir = settings.groups_dir / request.name
            group_dir.mkdir(parents=True, exist_ok=True)

            command_center = settings.command_center.connection
            if not command_center:
                logger.warning("create_periodic_agent requires command_center.connection")
                return

            workspace_config.add_workspace_to_toml(
                request.name,
                WorkspaceConfig.model_validate({"profiles": [request.profile]}),
            )
            workspace_config.add_job_to_toml(
                request.name,
                JobConfig.model_validate(
                    {
                        "workspace": request.name,
                        "schedule": request.schedule,
                        "prompt": request.prompt,
                    }
                ),
            )

            claude_md_path = group_dir / "CLAUDE.md"
            if not claude_md_path.exists():
                claude_md_path.write_text(request.claude_md)

            channel = _command_center_channel(metadata_manager.channels(), command_center)
            if channel is None:
                logger.warning(
                    "Command center does not support create_group; "
                    "periodic agent was created without chat"
                )
                return

            jid = _valid_jid(await channel.create_group(request.chat or request.name))
            if jid is None:
                logger.warning(
                    "Command center returned invalid jid for periodic agent",
                    name=request.name,
                    chat=request.chat or request.name,
                )
                return

            registration_manager.register_workspace(
                WorkspaceProfile(
                    jid=jid,
                    name=request.name.replace("-", " ").title(),
                    folder=request.name,
                    trigger="@pynchy",
                    added_at=datetime.now(UTC).isoformat(),
                )
            )
            task_id = f"periodic-{request.name}-{uuid4().hex[:8]}"
            await create_task(
                ScheduledTask(
                    id=task_id,
                    group_folder=request.name,
                    chat_jid=jid,
                    prompt=request.prompt,
                    schedule_type="cron",
                    schedule_value=request.schedule,
                    session_policy=SessionPolicy.RESET_BEFORE_RUN,
                    status="active",
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
            logger.info(
                "Periodic agent created via IPC",
                name=request.name,
                schedule=request.schedule,
                task_id=task_id,
                jid=jid,
            )

        async def get_scheduled_work_status(
            self,
            *,
            source_group: str,
            is_admin: bool,
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            return await _scheduled_work_status(
                source_group,
                is_admin=is_admin,
            )

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
    settings = get_settings()
    configured_repo_slugs = tuple(
        dict.fromkeys(
            [
                *settings.repos.overrides,
                *(slug for profile in settings.profiles.values() for slug in profile.repo),
            ]
        )
    )

    class _StatusDeps:
        repo_slugs = configured_repo_slugs
        temporal_address = settings.scheduler.temporal_address
        temporal_namespace = settings.scheduler.temporal_namespace
        temporal_task_queue = settings.scheduler.temporal_task_queue

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
                "max_concurrent": settings.container.max_concurrent,
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
