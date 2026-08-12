"""Dependency adapter factories compose subsystem dependencies from app state."""

from __future__ import annotations

# allow: file-length - local IPC composition binds the startup-owned snapshot data directory.
import subprocess  # noqa: S404 - status adapter catches Docker command timeouts.
from collections.abc import (
    Awaitable,  # noqa: TC003 - beartype resolves factory annotations at runtime.
    Callable,  # noqa: TC003 - beartype resolves factory annotations at runtime.
    Sequence,  # noqa: TC003 - beartype resolves channel collections at runtime.
)
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable
from uuid import uuid4

import pynchy.host.container_manager.gateway as gateway_manager
from pynchy.agent_home import is_skill_selected, parse_skill_tier
from pynchy.async_tasks import create_background_task
from pynchy.canaries.api import canary_run_to_dict, get_canary_report
from pynchy.config.api import JobConfig, Settings, WorkspaceConfig, apply_tool_access, get_settings
from pynchy.host.container_manager.docker import run_docker
from pynchy.host.container_manager.ipc.deps import (  # noqa: TC001 - beartype resolves dependency factory annotations at runtime.
    IpcDeps,
)
from pynchy.host.container_manager.ipc.protocol import (  # noqa: TC001 - beartype resolves dependency factory annotations at runtime.
    CreatePeriodicAgentRequest,
)
from pynchy.host.container_manager.security.cop import (
    CopInspectionContext,
)
from pynchy.host.container_manager.security.cop import (
    load_cop_inspection_context as build_cop_inspection_context,
)
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    build_workspace_security,
    evaluate_host_action_policy,
)
from pynchy.host.git_ops.api import (
    # beartype resolves dependency factory annotations at runtime.
    GitSyncDeps,
    count_unpushed_commits,
    detect_main_branch,
    files_changed_between,
    get_deploy_config_hash,
    get_head_commit_message,
    get_head_sha,
    get_repo_context,
    is_repo_dirty,
    push_local_commits,
    run_git,
)
from pynchy.host.learning.api import find_personalized_skill_dir
from pynchy.host.orchestrator import session_handler, workspace_config
from pynchy.host.orchestrator.action_intents import (
    execute_action_intent,
    policy_approval_timestamp,
    prepare_action_intent,
)
from pynchy.host.orchestrator.adapters import (
    GroupMetadataManager,
    GroupRegistrationManager,
    HostMessageBroadcaster,
    MessageBroadcaster,
    SessionManager,
    resolve_admin_notification_jid,
)
from pynchy.host.orchestrator.app import (  # noqa: TC001 - beartype resolves dependency factory annotations at runtime.
    PynchyApp,
)
from pynchy.host.orchestrator.capability_status import (
    CapabilityPolicyDecision,
    CapabilityStatusOperations,
    WorkspaceCapabilityConfiguration,
)
from pynchy.host.orchestrator.http_server import (
    # beartype resolves dependency factory annotations at runtime.
    HttpDeployOperations,
    HttpServerDeps,
)
from pynchy.host.orchestrator.messaging import pending_questions
from pynchy.host.orchestrator.scheduled_work_status import collect_scheduled_work
from pynchy.host.orchestrator.source_health_deps import SourceHealthProjection
from pynchy.host.orchestrator.status import (
    # beartype resolves dependency factory annotations at runtime.
    GitStatusOperations,
    StatusDeps,
)
from pynchy.host.orchestrator.task_scheduler import (  # noqa: TC001 - beartype resolves dependency factory annotations at runtime.
    SchedulerDependencies,
)
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.host.orchestrator.temporal.scheduler import (
    start_deploy_workflow,
    start_scheduled_agent_task_workflow,
)
from pynchy.host.orchestrator.temporal.status import get_temporal_orchestration_states
from pynchy.host.orchestrator.terminal_task_retirement import (
    retire_conversation_tasks,
    retire_provider_work_item_execution,
)
from pynchy.identifiers import (  # noqa: TC001 - beartype resolves nested adapter annotations.
    GroupFolder,
    SessionId,
)
from pynchy.ipc_snapshots import write_groups_snapshot as _write_groups_snapshot
from pynchy.logger import logger
from pynchy.plugins.api import (  # beartype resolves dependency adapter annotations at runtime.
    Channel,
    HostActionDescriptor,
    NewMessage,
)
from pynchy.plugins.integrations.api import work_item_execution_to_dict
from pynchy.plugins.speech.api import (  # noqa: TC001 - beartype resolves dependency factory annotations at runtime.
    SpeechSynthesizer,
)
from pynchy.scheduling.api import (  # beartype resolves dependency adapter annotations at runtime.
    HostJob,
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state.api import (
    approve_action_intent,
    complete_conversation_delivery,
    conversation_control_state_matches,
    create_host_job,
    create_task,
    delete_host_job,
    delete_task,
    deny_action_intent,
    expire_action_intent,
    fail_action_intent,
    get_action_intent_by_request,
    get_all_host_jobs,
    get_all_tasks,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_control_by_thread,
    get_host_job_by_id,
    get_task_by_id,
    get_task_run_logs,
    get_terminal_conversation_retirement,
    load_recent_security_context,
    mark_action_intent_awaiting_approval,
    resume_task,
    update_host_job,
    update_task,
)
from pynchy.work_items.api import (  # noqa: TC001 - beartype resolves factory annotations.
    WorkItemExecution,
)
from pynchy.workspace.api import (  # beartype resolves dependency adapter annotations at runtime.
    WorkspaceProfile,
    WorkspaceSecurity,
)

if TYPE_CHECKING:
    from pynchy.host.orchestrator.webhook_terminal_retirement import (
        TerminalConversationRetirementDeps,
    )


def _get_broadcasters(app: PynchyApp) -> tuple[MessageBroadcaster, HostMessageBroadcaster]:
    """Return the app's shared broadcaster pair.

    All subsystems reuse the same MessageBroadcaster and HostMessageBroadcaster
    instances from PynchyApp, ensuring a single code path for all channel sends.
    """
    return app.message_broadcaster, app.host_broadcaster


async def _load_cop_inspection_context(chat_jid: str) -> CopInspectionContext:
    """Build the bounded security projection from the durable store."""
    return await build_cop_inspection_context(chat_jid, load_recent_security_context)


def _skill_access_status(group_folder: str, skill_name: str) -> str:
    """Project one learned skill's current access state for IPC."""
    skill_dir = find_personalized_skill_dir(skill_name)
    if skill_dir is None:
        return "unknown"
    resolved = workspace_config.load_resolved_config(group_folder)
    if resolved is None:
        return "unavailable"
    if skill_name in resolved.denied_skills:
        return "denied"
    name, tier = parse_skill_tier(skill_dir)
    return "granted" if is_skill_selected(name, tier, resolved.skills) else "available"


def _schedule_interactive_turn(app: PynchyApp, chat_jid: str) -> None:
    create_background_task(
        app.start_interactive_turn(chat_jid),
        name=f"interactive-turn-{chat_jid[:20]}",
    )


def _capability_status_operations(settings: Settings) -> CapabilityStatusOperations:
    """Compose the fixed workspace policy projection used by status endpoints."""

    def workspace_configuration(workspace: str) -> WorkspaceCapabilityConfiguration | None:
        resolved = settings.resolved_workspace_config(workspace)
        if resolved is None:
            return None
        resolved = apply_tool_access(settings.tools, resolved)[0]
        return WorkspaceCapabilityConfiguration(
            enabled_tools=frozenset(resolved.tools),
            security=build_workspace_security(settings, resolved),
        )

    def evaluate_action_policy(
        action: HostActionDescriptor,
        security: WorkspaceSecurity,
    ) -> CapabilityPolicyDecision:
        decision = evaluate_host_action_policy(action, SecurityGate(security), {})
        return CapabilityPolicyDecision(
            allowed=decision.allowed,
            reason=decision.reason,
            approval_required=decision.needs_human,
            cop_review_required=decision.needs_cop,
        )

    return CapabilityStatusOperations(
        workspaces=tuple(settings.workspaces),
        workspace_configuration=workspace_configuration,
        evaluate_action_policy=evaluate_action_policy,
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
        capability_status_operations = _capability_status_operations(settings)
        deploy_operations = HttpDeployOperations(
            get_head_sha=get_head_sha,
            push_local_commits=push_local_commits,
            run_git=run_git,
            files_changed_between=files_changed_between,
            get_deploy_config_hash=get_deploy_config_hash,
            get_head_commit_message=get_head_commit_message,
            is_repo_dirty=is_repo_dirty,
            start_deploy_workflow=start_deploy_workflow,
        )
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

        get_canary_report = staticmethod(get_canary_report)
        canary_run_to_dict = staticmethod(canary_run_to_dict)
        work_item_execution_to_dict = staticmethod(work_item_execution_to_dict)

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
            await app.retire_workspace_runtime(folder)

        retire_conversation_tasks = staticmethod(retire_conversation_tasks)

        async def ingest_message(self, jid: str, message: NewMessage) -> None:
            await app.on_inbound(jid, message)

    return HttpDeps()


def make_provider_execution_retirement(
    app: PynchyApp,
) -> Callable[[WorkItemExecution, str | None], Awaitable[None]]:
    """Bind provider terminal cleanup to the app's shared HTTP runtime adapters."""
    deps = cast("TerminalConversationRetirementDeps", make_http_deps(app))
    return partial(retire_provider_work_item_execution, deps)


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

        wake_worktree_conflict = app.start_interactive_turn
        clear_chat_history = registration_manager.clear_chat_history
        channels = metadata_manager.channels
        pending_question_store = staticmethod(_PendingQuestionStore)
        scheduled_work_store = staticmethod(_ScheduledWorkStore)
        messaging_source_health = staticmethod(SourceHealthProjection)
        prepare_action_intent = staticmethod(prepare_action_intent)
        execute_action_intent = staticmethod(execute_action_intent)
        policy_approval_timestamp = staticmethod(policy_approval_timestamp)
        approve_action_intent = staticmethod(approve_action_intent)
        deny_action_intent = staticmethod(deny_action_intent)
        fail_action_intent = staticmethod(fail_action_intent)
        expire_action_intent = staticmethod(expire_action_intent)
        mark_action_intent_awaiting_approval = staticmethod(mark_action_intent_awaiting_approval)
        get_conversation_control_by_thread = staticmethod(get_conversation_control_by_thread)
        get_action_intent_by_request = staticmethod(get_action_intent_by_request)
        get_conversation_control_binding = staticmethod(get_conversation_control_binding)
        load_cop_inspection_context = staticmethod(_load_cop_inspection_context)
        sweep_expired_questions = staticmethod(pending_questions.sweep_expired_questions)
        skill_access_status = staticmethod(_skill_access_status)
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
                        "memory": request.memory_enabled,
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
                    memory_enabled=request.memory_enabled,
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
        capability_status_operations = _capability_status_operations(settings)
        repo_slugs = configured_repo_slugs
        temporal_address = settings.scheduler.temporal_address
        temporal_namespace = settings.scheduler.temporal_namespace
        temporal_task_queue = settings.scheduler.temporal_task_queue
        git_status = GitStatusOperations(
            get_repo_context=get_repo_context,
            get_head_sha=get_head_sha,
            is_repo_dirty=is_repo_dirty,
            count_unpushed_commits=count_unpushed_commits,
            get_head_commit_message=get_head_commit_message,
            detect_main_branch=detect_main_branch,
            run_git=run_git,
        )

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
            response_info = (
                {"responses": gw.responses_status}
                if isinstance(gw, gateway_manager.LiteLLMGateway)
                else {}
            )
            return {
                "mode": mode,
                "port": gw.port,
                "key": gw.key,
                "redaction": gw.redaction_posture.value,
                **response_info,
            }

        async def get_container_state(self, name: str) -> str:
            """Return a best-effort container state for the status endpoint."""
            try:
                result = await run_docker("inspect", "-f", "{{.State.Status}}", name, check=False)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                return "not_found"
            return result.stdout.strip() if result.returncode == 0 else "not_found"

        def get_active_sessions_count(self) -> int:
            active = session_manager.get_active_sessions(app.workspaces)
            return len(active)

        def get_workspace_count(self) -> int:
            return len(app.workspaces)

        def get_speech_synthesizer(self) -> SpeechSynthesizer | None:
            return app.get_speech_synthesizer()

        get_canary_report = staticmethod(get_canary_report)

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

        async def wake_worktree_conflict(self, jid: str) -> None:
            await app.start_interactive_turn(jid)

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
