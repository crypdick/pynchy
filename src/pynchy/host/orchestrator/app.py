"""Main orchestrator — owns runtime state and wires subsystems together.

Lifecycle (startup phases, shutdown) lives in :mod:`lifecycle`.
# allow: file-length - composition root exposes lifecycle and channel adapter methods.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import (  # noqa: TC003 - beartype resolves composition callback annotations.
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path  # noqa: TC003 - beartype resolves application method annotations.
from threading import Lock
from typing import Any, cast

import pluggy  # noqa: TC002 - beartype resolves app annotations at runtime.

from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    ContainerInput,
    ContainerOutput,
)
from pynchy.atomic_json import write_json_atomic
from pynchy.canaries.api import CanaryRuntime, configure_canary_runtime, run_declared_canaries
from pynchy.canary_contracts import (  # noqa: TC001 - beartype resolves method annotations.
    CanaryRun,
)
from pynchy.config.api import (
    JobConfig,
    Settings,
    WorkspaceConfig,
    access,
    apply_tool_access,
    automation_projection,
    configuration_source_digest,
    get_settings,
    load_prompt_catalog,
    load_runtime_candidate,
    mutate_config_toml,
    parse_chat_ref,
    publish_settings,
    read_prompt,
    reset_settings,
    resolve_tool_access,
    restart_fingerprint,
    runtime_policy_changes,
    tool_process_environment,
    validate_settings_mapping,
)
from pynchy.conversation.api import (
    Conversation,
    ConversationId,
    conversation_id_from_folder,
)
from pynchy.event_bus import Event, EventBus
from pynchy.host.container_manager.api import (
    AgentHomeMounts,  # beartype resolves composition contracts at runtime.
    McpStartupFailure,  # beartype resolves contract annotations at runtime.
    RepoMount,
    RepoMountResolution,
)
from pynchy.host.container_manager.credentials import (
    build_agent_env_vars,
    configure_workspace_environment,
    has_api_credentials,
)
from pynchy.host.container_manager.gateway import GatewaySettings, configure_gateway_runtime
from pynchy.host.container_manager.ipc.handlers_approval import (
    ApprovalSettings,
    configure_approval_runtime,
    process_approval_decision,
)
from pynchy.host.container_manager.ipc.handlers_lifecycle import (
    LifecycleRuntime,
    LifecycleSettings,
    PublicationRepositoryError,
    RepoContext,
    configure_lifecycle_runtime,
)
from pynchy.host.container_manager.ipc.handlers_managed_feature import (
    ManagedFeatureRepoContext,
    ManagedFeatureRuntime,
    ManagedFeatureSettings,
    configure_managed_feature_runtime,
)
from pynchy.host.container_manager.ipc.handlers_service import (
    ServiceSettings,
    configure_service_runtime,
)
from pynchy.host.container_manager.ipc.skill_access import persist_skill_access_choice
from pynchy.host.container_manager.ipc.write import (
    clean_ipc_input_dir,
    configure_ipc_base_dir,
    ipc_response_path,
    write_ipc_close_sentinel,
    write_ipc_message,
    write_ipc_response,
)
from pynchy.host.container_manager.mcp.approval import (  # noqa: TC001 - beartype resolves callback request annotations at runtime.
    McpApprovalRequest,
)
from pynchy.host.container_manager.mcp.manager import (
    configure_mcp_manager_runtime,
    get_mcp_manager,
)
from pynchy.host.container_manager.mcp.resolution import (
    ResolvedMcpWorkspace,
    configure_mcp_resolution_runtime,
)
from pynchy.host.container_manager.mounts import MountOperations, configure_mount_operations
from pynchy.host.container_manager.orchestrator import (
    _spawn_container,
    configure_container_spawn_runtime,
    stable_container_name,
)
from pynchy.host.container_manager.process import (
    configure_container_process_runtime,
    docker_rm_force,
    graceful_stop,
)
from pynchy.host.container_manager.security.approval import (
    _approval_decisions_dir,
    approval_event,
    configure_approval_state_root,
    create_pending_approval,
    find_pending_by_short_id,
    list_pending_approvals,
)
from pynchy.host.container_manager.security.audit import (
    configure_security_audit_storage,
    record_security_event,
)
from pynchy.host.container_manager.security.cop import configure_cop_prompt_provider
from pynchy.host.container_manager.security.cop_client import configure_cop_gateway
from pynchy.host.container_manager.security.gate import (
    ResolvedSecurityConfig,
    SecuritySettings,
    configure_security_resolution,
    create_gate,
    destroy_gate,
    get_gate_for_group,
    resolve_security,
)
from pynchy.host.container_manager.session import (
    SessionDiedError,
    active_session_group_folders,
    create_session,
    destroy_all_sessions,
    destroy_session,
    get_session,
)
from pynchy.host.git_ops.api import (
    GitSyncRuntime,
    RepoSettings,
    ResolvedRepoWorkspace,
    RoutedHostWorktreeError,
    check_local_head_drift,
    check_origin_drift,
    configure_git_sync_runtime,
    configure_repo_runtime,
    count_unpushed_commits,
    detect_main_branch,
    find_pynchy_repo_ctx,
    get_deploy_config_hash,
    get_head_commit_message,
    get_head_sha,
    get_local_head_sha,
    get_repo_context,
    git_env_with_token,
    host_create_pr_from_managed_feature,
    host_create_pr_from_worktree,
    host_get_origin_main_sha,
    host_notify_worktree_updates,
    host_rebase_managed_feature,
    host_update_main,
    host_update_main_result,
    is_repo_dirty,
    last_notified_sha,
    needs_container_rebuild,
    needs_deploy,
    probe_origin_main_sha,
    prune_stale_worktree_venvs,
    read_managed_feature_patch,
    redact_git_diagnostic,
    repo_container_path,
    repo_host_root,
    resolve_managed_feature_publication,
    resolve_repos_for_group,
    resolve_routed_host_worktree_cwd,
    run_git,
)
from pynchy.host.git_ops.utils import configure_git_default_cwd
from pynchy.host.git_ops.worktree import (
    WorktreeStartupRuntime,
    configure_worktree_startup_runtime,
    ensure_worktree,
)
from pynchy.host.learning.api import (
    LearningPathsRuntime,
    automation_memory_dir,
    configure_learning_paths_runtime,
    prepare_agent_homes,
    profile_name_for_group,
    refresh_personalized_agent_skills,
    resolve_learning_paths,
)
from pynchy.host.learning.api import (
    capture as learning_capture,
)
from pynchy.host.learning.api import (
    run_learning_review as run_host_learning_review,
)
from pynchy.host.learning.skill_activation import (
    SkillActivationRuntime,
    configure_skill_activation_runtime,
)
from pynchy.host.learning.skills import configure_personalized_skills_root
from pynchy.host.orchestrator import (
    agent_runner,
    host_execution,
    linear_issue_controls,
    linear_plan_review,
    session_handler,
    update_offer,
)
from pynchy.host.orchestrator.adapters import (
    broadcast_host_message as send_host_message,
)
from pynchy.host.orchestrator.adapters import (
    broadcast_system_notice as send_system_notice,
)
from pynchy.host.orchestrator.concurrency import GroupQueue, QueuePolicy
from pynchy.host.orchestrator.config_refresh import (
    ConfigRefreshRuntime,
    configure_config_refresh_runtime,
    refresh_host_config,
)
from pynchy.host.orchestrator.connection_runtime_owner import ConnectionRuntimeOwner
from pynchy.host.orchestrator.deploy import DeployGitRuntime, configure_deploy_git_runtime
from pynchy.host.orchestrator.job_sources import (
    PluginJobsRuntime,
    configure_plugin_jobs_runtime,
)
from pynchy.host.orchestrator.mcp_notifications import notify_mcp_startup_failures
from pynchy.host.orchestrator.messaging import (
    ask_user_handler,
    channel_handler,
    pending_questions,
    reaction_handler,
)
from pynchy.host.orchestrator.messaging import pipeline as message_handler
from pynchy.host.orchestrator.messaging import (
    router as output_handler,
)
from pynchy.host.orchestrator.messaging.ask_user_handler import AskUserRuntimeOperations
from pynchy.host.orchestrator.messaging.cursor import monotonic_cursor
from pynchy.host.orchestrator.messaging.deps import (  # beartype resolves method annotations.
    ApprovalRuntimeOperations,
    CommandMatcher,
    DirectCommandOutput,
)
from pynchy.host.orchestrator.messaging.reconciler import configure_allowed_message_filter
from pynchy.host.orchestrator.messaging.sender import broadcast
from pynchy.host.orchestrator.messaging.sender_policy import load_allowed_group_messages
from pynchy.host.orchestrator.runtime_process_control import ContainerRuntimeOperations
from pynchy.host.orchestrator.runtime_task_owner import RuntimeTaskOwner
from pynchy.host.orchestrator.scheduler_deps import (  # beartype resolves method annotations.
    ConfigHostCronJob,
    ScheduledExecutionLifecycle,
    SchedulerRuntimeConfig,
)
from pynchy.host.orchestrator.startup_handler import (
    StartupRuntime,
    StartupSettings,
    configure_startup_runtime,
)
from pynchy.host.orchestrator.startup_readiness import StartupReadiness
from pynchy.host.orchestrator.temporal import scheduler as temporal_scheduler
from pynchy.host.orchestrator.temporal.git_sync import (
    TemporalGitSyncRuntime,
    configure_temporal_git_sync_runtime,
)
from pynchy.host.orchestrator.thread_routing import ThreadRouting
from pynchy.host.orchestrator.workspace_artifacts import (
    cleanup_startup_workspace_artifacts,
    cleanup_workspace_artifacts,
)
from pynchy.host.orchestrator.workspace_config import (
    WorkspaceConfigRuntime,
    configure_workspace_config_runtime,
    load_resolved_config,
    load_resolved_tool_access,
    reconcile_automation_jobs,
    static_workspace_folder,
    update_profile_skill_policy,
)
from pynchy.host.orchestrator.workspace_placement import configure_workspace_placement
from pynchy.host.orchestrator.workspace_registration import (
    available_workspace_groups,
    configure_workspace_registration_runtime,
    rebind_workspace_runtime,
    workspace_security,
)
from pynchy.host.orchestrator.workspace_threads import configure_workspace_threads_runtime
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    RuntimeId,
    SessionId,
)
from pynchy.learning_packets import (  # noqa: TC001 - beartype resolves method annotations.
    LearningPacket,
)
from pynchy.linear_plan_types import (  # noqa: TC001 - beartype resolves app annotations at runtime.
    LinearPlanReviewAdmission,
    LinearPlanReviewRequest,
    LinearPlanReviewResult,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    # beartype resolves app annotations at runtime.
    Channel,
    NewMessage,
    ObserverProvider,
    OutboundEvent,
    prepare_context_reset,
)
from pynchy.plugins.integrations.api import (
    LinearIssueControl,
    attach_work_item_pull_request,
    create_linear_workspace_todo,
    get_active_matrix_route,
    linear_workspace_boards,
    linear_workspace_enabled,
    reconcile_all_linear_work_items,
    resolve_linear_issue_conversation,
)
from pynchy.plugins.integrations.api import (
    process_linear_plan_review_admission as admit_linear_plan_review,
)
from pynchy.plugins.runtimes.api import (
    configure_runtime_override,
    ensure_agent_image_available,
    get_runtime,
)
from pynchy.plugins.speech.api import (  # noqa: TC001 - beartype resolves app annotations at runtime.
    SpeechSynthesizer,
)
from pynchy.scheduling.api import (
    ScheduledTask,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.state.api import (
    cancel_task_and_checkpoint,
    clear_runtime_session_references_batch,
    clear_session,
    delete_workspace_profile,
    get_all_chats,
    get_all_sessions,
    get_all_workspace_profiles,
    get_conversation,
    get_conversation_control_by_thread,
    get_in_flight_turn_for_group,
    get_latest_canary_runs,
    get_recent_canary_runs,
    get_router_state,
    get_unfinished_work_item_execution,
    get_unresolved_canary_regressions,
    get_work_item_execution_for_task,
    get_work_item_execution_for_turn,
    prune_messages_by_sender,
    record_canary_run,
    save_router_state_batch,
    set_session,
    set_workspace_profile,
    set_workspace_profiles,
    store_message,
    store_message_direct,
    update_task,
    upgrade_message_cursor,
)
from pynchy.turn_outcomes import (  # noqa: TC001 - beartype resolves this result annotation.
    TurnOutcome,
)
from pynchy.workspace.api import RuntimeTarget, WorkspaceProfile


async def _fresh_container_name(group_folder: str) -> str:
    """Remove a stale durable worker before replacing it with the same name."""
    container_name = stable_container_name(group_folder)
    await docker_rm_force(container_name)
    return container_name


async def _ensure_workspace_mcp(group_folder: str) -> tuple[McpStartupFailure, ...]:
    """Ensure optional MCP tools for a durable worker and return their failures."""
    if (manager := get_mcp_manager()) is None:
        return ()
    return (await manager.ensure_workspace_running(group_folder)).failures


async def _wait_for_agent_query(session: object, query_timeout_seconds: float) -> bool:
    """Wait for a container query, distinguishing expected worker death."""
    try:
        await cast("agent_runner.AgentSession", session).wait_for_query_done(
            query_timeout_seconds=query_timeout_seconds
        )
    except SessionDiedError:
        return False
    return True


def _resolve_repo_mounts(
    group_folder: str,
    repo_accesses: tuple[str, ...],
) -> RepoMountResolution:
    mounts: list[RepoMount] = []
    notices: list[str] = []
    for slug in repo_accesses:
        repo_context = get_repo_context(slug)
        if repo_context is None:
            continue
        worktree = ensure_worktree(group_folder, repo_context)
        mounts.append(
            RepoMount(
                slug=repo_context.slug,
                root=repo_context.root,
                worktree_path=worktree.path,
            )
        )
        notices.extend(worktree.notices)
    return RepoMountResolution(tuple(mounts), tuple(notices))


def _mount_agent_homes(
    group_folder: str,
    plugin_manager: pluggy.PluginManager | None,
) -> AgentHomeMounts:
    homes = prepare_agent_homes(group_folder, plugin_manager)
    return AgentHomeMounts(
        claude_home=homes.claude_home,
        codex_home=homes.codex_home,
        vault_mount_root=homes.learning_paths.vault_root if homes.learning_paths else None,
        vault_mount_path=(
            homes.learning_paths.vault_mount_path if homes.learning_paths is not None else None
        ),
    )


def _workspace_environment(
    settings: Settings,
    *,
    is_admin: bool,
    group_folder: str,
) -> dict[str, str]:
    access = load_resolved_tool_access(group_folder)
    env_vars = dict(access.workspace_env) if access is not None else {}
    if is_admin:
        chrome_profiles = settings.chrome_profiles
    else:
        chrome_profiles_set: set[str] = set()
        resolved = load_resolved_config(group_folder)
        for tool_name in resolved.tools if resolved else []:
            tool = settings.tools.get(tool_name)
            if tool is None or tool.type != "mcp" or "." not in tool_name:
                continue
            _, instance_name = tool_name.split(".", 1)
            if instance_name in settings.chrome_profiles:
                chrome_profiles_set.add(instance_name)
        chrome_profiles = sorted(chrome_profiles_set)
    if chrome_profiles:
        env_vars["PYNCHY_CHROME_PROFILES"] = ",".join(chrome_profiles)
    return env_vars


def _resolve_security_workspace_config(
    folder: str,
    settings: SecuritySettings | None,
) -> ResolvedSecurityConfig | None:
    """Resolve container security from the exact configuration snapshot in use."""
    return cast(
        "ResolvedSecurityConfig | None",
        load_resolved_config(
            folder,
            settings=cast("Settings | None", settings),
        ),
    )


def _resolve_mcp_workspace_config(
    folder: str,
    settings: object,
) -> ResolvedMcpWorkspace | None:
    """Resolve effective MCP tools from the manager's configuration snapshot."""
    return cast(
        "ResolvedMcpWorkspace | None",
        load_resolved_config(folder, settings=cast("Settings", settings)),
    )


def _read_selected_prompts(names: list[str]) -> str | None:
    settings = get_settings()
    return load_prompt_catalog(
        default_prompts=settings.project_root / "data/defaults/prompts",
        personalized_prompts=settings.project_root / "data/personalization/prompts",
    ).compose(names)


def _read_current_prompt(field: str) -> str:
    settings = get_settings()
    return read_prompt(getattr(settings.prompts, field), settings.project_root)


def _configure_container_policy_runtime(*, is_apple_container: bool) -> None:
    """Wire container policy readers to the current host configuration."""
    configure_workspace_config_runtime(
        WorkspaceConfigRuntime(
            get_settings=get_settings,
            read_prompts=_read_selected_prompts,
            parse_workspace_config=WorkspaceConfig.model_validate,
            apply_tool_access=apply_tool_access,
            resolve_tool_access=resolve_tool_access,
            mutate_config_toml=mutate_config_toml,
            validate_settings_mapping=validate_settings_mapping,
            reset_settings=reset_settings,
        )
    )
    configure_workspace_registration_runtime(parse_chat_reference=parse_chat_ref)
    configure_workspace_threads_runtime(settings=get_settings)
    configure_plugin_jobs_runtime(
        PluginJobsRuntime(
            get_settings=get_settings,
            parse_job=JobConfig.model_validate,
        )
    )
    configure_temporal_git_sync_runtime(
        TemporalGitSyncRuntime(
            get_settings=get_settings,
            check_local_head_drift=check_local_head_drift,
            check_origin_drift=check_origin_drift,
            find_pynchy_repo_ctx=find_pynchy_repo_ctx,
            get_deploy_config_hash=get_deploy_config_hash,
            get_local_head_sha=get_local_head_sha,
            get_repo_context=get_repo_context,
            git_env_with_token=git_env_with_token,
            host_get_origin_main_sha=host_get_origin_main_sha,
            host_notify_worktree_updates=host_notify_worktree_updates,
            host_update_main_result=host_update_main_result,
            last_notified_sha=last_notified_sha,
            needs_deploy=needs_deploy,
            probe_origin_main_sha=probe_origin_main_sha,
            prune_stale_worktree_venvs=prune_stale_worktree_venvs,
            refresh_host_config=refresh_host_config,
        )
    )
    configure_startup_runtime(
        StartupRuntime(
            get_settings=cast("Callable[[], StartupSettings]", get_settings),
            head_commit_message=get_head_commit_message,
            head_sha=get_head_sha,
            repo_dirty=is_repo_dirty,
            git=run_git,
        )
    )
    configure_repo_runtime(
        get_settings=cast("Callable[[], RepoSettings]", get_settings),
        resolve_workspace_config=cast(
            "Callable[[str], ResolvedRepoWorkspace | None]", load_resolved_config
        ),
    )
    configure_mcp_resolution_runtime(
        apply_tool_access=cast(
            "Callable[[Mapping[str, object], object], tuple[ResolvedMcpWorkspace, object]]",
            apply_tool_access,
        ),
        tool_process_environment=cast(
            "Callable[[object], dict[str, str]]", tool_process_environment
        ),
    )
    configure_mcp_manager_runtime(
        static_workspace_folder=static_workspace_folder,
        load_resolved_workspace_config=_resolve_mcp_workspace_config,
    )
    configure_gateway_runtime(
        is_apple_container=is_apple_container,
        get_settings=cast("Callable[[], GatewaySettings]", get_settings),
    )
    configure_security_resolution(
        get_settings=cast("Callable[[], SecuritySettings]", get_settings),
        resolve_workspace_config=_resolve_security_workspace_config,
    )
    configure_approval_runtime(get_settings=cast("Callable[[], ApprovalSettings]", get_settings))
    configure_service_runtime(
        get_settings=cast("Callable[[], ServiceSettings]", get_settings),
        resolve_workspace_config=_resolve_security_workspace_config,
        active_matrix_route=get_active_matrix_route,
    )

    def resolve_publication_repos(folder: str, turn_id: str | None) -> Sequence[RepoContext]:
        """Select host-authorized repositories for an active publication request."""
        route = host_execution.active_routed_host_repo(folder)
        if route is not None:
            if turn_id != route.turn_id:
                raise PublicationRepositoryError(
                    "Routed host publication does not match the active turn."
                )
            try:
                repo_context = get_repo_context(route.repo_access)
            except ValueError as exc:
                raise PublicationRepositoryError(
                    "Routed host turn selected an unavailable repository."
                ) from exc
            if repo_context is None:
                raise PublicationRepositoryError(
                    "Routed host turn selected an unavailable repository."
                )
            return (repo_context,)

        resolved = load_resolved_config(folder)
        if resolved is None and conversation_id_from_folder(folder) is not None:
            raise PublicationRepositoryError(
                "Routed publication has no active workspace configuration."
            )
        if (
            resolved is None
            or resolved.execution_mode != "host"
            or not resolved.cwd
            or conversation_id_from_folder(folder) is None
        ):
            return resolve_repos_for_group(folder)

        raise PublicationRepositoryError(
            "Routed host turn is no longer active; refusing to publish a stale request."
        )

    configure_lifecycle_runtime(
        LifecycleRuntime(
            settings=cast("Callable[[], LifecycleSettings]", get_settings),
            resolve_publication_repos=resolve_publication_repos,
            get_work_item_execution_for_turn=get_work_item_execution_for_turn,
            get_work_item_execution_for_task=get_work_item_execution_for_task,
            get_unfinished_work_item_execution=get_unfinished_work_item_execution,
            get_current_turn=get_in_flight_turn_for_group,
            get_conversation=get_conversation,
            attach_work_item_pull_request=attach_work_item_pull_request,
            detect_main_branch=detect_main_branch,
            host_create_pr_from_worktree=host_create_pr_from_worktree,
            redact_git_diagnostic=redact_git_diagnostic,
            run_git=run_git,
        )
    )
    configure_managed_feature_runtime(
        ManagedFeatureRuntime(
            settings=cast("Callable[[], ManagedFeatureSettings]", get_settings),
            resolve_repos_for_group=cast(
                "Callable[[str], Sequence[ManagedFeatureRepoContext]]", resolve_repos_for_group
            ),
            resolve_managed_feature_publication=resolve_managed_feature_publication,
            read_managed_feature_patch=read_managed_feature_patch,
            host_create_pr_from_managed_feature=host_create_pr_from_managed_feature,
            host_rebase_managed_feature=host_rebase_managed_feature,
            redact_git_diagnostic=redact_git_diagnostic,
        )
    )


async def _prepare_host_direct_mcp_servers(
    input_data: ContainerInput,
    group_folder: str,
    chat_jid: str,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
) -> None:
    """Register host security context, then attach selected MCP proxy routes."""
    invocation_ts = time.monotonic()
    security = resolve_security(group_folder, is_admin=input_data.is_admin)
    create_gate(
        group_folder,
        invocation_ts,
        security,
        public_source_input=input_data.corruption_tainted,
        secret_source_input=input_data.secret_tainted,
    )
    input_data.invocation_ts = invocation_ts
    if (manager := get_mcp_manager()) is None:
        return
    mcp_startup = await manager.ensure_workspace_running(group_folder)
    if mcp_startup.failures:
        await notify_mcp_startup_failures(
            broadcast_host_message,
            chat_jid,
            mcp_startup.failures,
        )
    input_data.mcp_direct_servers = manager.get_direct_server_configs(
        group_folder,
        invocation_ts=invocation_ts,
        instance_ids=mcp_startup.ready_instance_ids,
    )


def _has_live_container_session(group_folder: GroupFolder) -> bool:
    session = get_session(group_folder)
    return session is not None and session.is_alive


def _write_ask_user_response(group_folder: str, request_id: str, result: dict[str, object]) -> None:
    write_ipc_response(ipc_response_path(group_folder, request_id), {"result": result})


def _persist_skill_access_choice(pending: dict[str, Any], answer: dict[str, Any]) -> str | None:
    """Bind learned-skill policy persistence at application composition."""
    return persist_skill_access_choice(
        pending,
        answer,
        profile_name_for_group=profile_name_for_group,
        update_profile_skill_policy=update_profile_skill_policy,
    )


async def _persist_and_process_approval(
    app: PynchyApp,
    source_group: str,
    decision_data: dict[str, object],
) -> None:
    """Persist an operator decision in host-only state, then replay it."""
    from pynchy.host.orchestrator.dep_factory import (  # noqa: PLC0415 - dep factory imports this composition root.
        make_ipc_deps,
    )

    decision_file = _approval_decisions_dir(source_group) / f"{decision_data['request_id']}.json"
    write_json_atomic(decision_file, decision_data, indent=2)
    await process_approval_decision(
        decision_file,
        source_group,
        deps=make_ipc_deps(app),
    )


def _scheduler_runtime_config(settings: Settings) -> SchedulerRuntimeConfig:
    """Project validated configuration into the scheduler's runtime contract."""
    config_host_cron_jobs: dict[str, ConfigHostCronJob] = {}
    for name, job in settings.jobs.items():
        if not job.is_host or not job.enabled:
            continue
        config_host_cron_jobs[name] = ConfigHostCronJob(
            command=cast("str", job.command),
            schedule=cast("str", job.schedule),
            cwd=job.cwd,
            timeout_seconds=job.timeout_seconds,
            quiet_on_success=job.quiet_on_success is True,
            memory_enabled=job.memory,
        )

    repo_slugs: set[str] = set()
    for workspace_name in settings.workspaces:
        resolved = cast("Any", settings.resolved_workspace_config(workspace_name))
        repo_slugs.update(resolved.repo)
    external_repo_sync_slugs = tuple(
        repo_slug
        for repo_slug in sorted(repo_slugs)
        if (repo_root := repo_host_root(cast("RepoSettings", settings), repo_slug)) is not None
        and repo_root.resolve() != settings.project_root.resolve()
    )

    return SchedulerRuntimeConfig(
        temporal_address=settings.scheduler.temporal_address,
        temporal_namespace=settings.scheduler.temporal_namespace,
        temporal_task_queue=settings.scheduler.temporal_task_queue,
        reconcile_schedules=settings.scheduler.reconcile_schedules,
        poll_interval=settings.scheduler.poll_interval,
        timezone=settings.timezone or None,
        git_sync_interval_seconds=settings.scheduler.git_sync_interval_seconds,
        channel_reconciliation_interval_seconds=settings.scheduler.channel_reconciliation_interval_seconds,
        auto_deploy=settings.scheduler.auto_deploy,
        idle_timeout=settings.idle_timeout,
        groups_dir=settings.groups_dir,
        project_root=settings.project_root,
        admin_workspace=settings.notifications.admin_workspace,
        queue_max_retries=settings.queue.max_retries,
        queue_base_retry_seconds=float(settings.queue.base_retry_seconds),
        learning_max_attempts=settings.learning.max_attempts,
        canary_enabled=settings.canary.enabled,
        canary_schedule=settings.canary.schedule,
        canary_target_profile=settings.canary.target_profile,
        canary_scenario_ids=tuple(settings.canary.scenario_ids),
        external_repo_sync_slugs=external_repo_sync_slugs,
        config_host_cron_jobs=config_host_cron_jobs,
    )


def _agent_execution_runtime_config(settings: Settings) -> AgentExecutionRuntime:
    return AgentExecutionRuntime(
        project_root=settings.project_root,
        groups_dir=settings.groups_dir,
        data_dir=settings.data_dir,
        mount_allowlist_path=settings.mount_allowlist_path,
        blocked_mount_patterns=tuple(settings.security.blocked_patterns),
        agent_image=settings.container.image,
        agent_memory_mb=settings.container.memory_mb,
        container_timeout=settings.container_timeout,
        default_core=settings.agent.default_core,
        idle_timeout=settings.idle_timeout,
        model=settings.agent.model,
        model_reasoning_effort=settings.agent.model_reasoning_effort,
    )


def _configure_learning_runtime(settings: Settings) -> None:
    def profile_for_workspace(folder: str) -> str | None:
        workspace = get_settings().workspaces.get(static_workspace_folder(folder))
        return workspace.profiles[0] if workspace is not None and workspace.profiles else None

    configure_learning_paths_runtime(
        LearningPathsRuntime(
            enabled=settings.learning.enabled,
            vault_root=settings.learning.obsidian.vault_root,
            vault_mount_path=settings.learning.obsidian.mount_path,
            default_profile_root=settings.learning.obsidian.default_profile_root,
            memory_dir_name=settings.learning.obsidian.memory_dir_name,
            profile_for_workspace=profile_for_workspace,
        )
    )


class PynchyApp(ThreadRouting):
    """Main application class — owns all runtime state and wires subsystems."""

    def __init__(self) -> None:
        self.last_timestamp: str = ""
        self.sessions: dict[str, str] = {}
        self.session_cleared: set[str] = set()  # group folders with pending clears
        self.workspaces: dict[str, WorkspaceProfile] = {}
        self.last_agent_timestamp: dict[str, str] = {}
        # Transient dispatch tracker — NOT persisted.  Resets to {} on every
        # restart so recover_pending_messages always uses last_agent_timestamp
        # (the true "successfully processed" cursor) as its baseline.
        self._dispatched_through: dict[str, str] = {}
        self.message_loop_running: bool = False
        # Approval decisions and scheduled polling share one personalization checkout.
        self._personalization_sync_lock = Lock()
        settings = get_settings()
        self._configure_runtime_dependencies(settings)
        self.message_data_dir = settings.data_dir
        self.message_poll_interval = settings.intervals.message_poll
        self.scheduler_runtime = _scheduler_runtime_config(settings)
        self._learning_review_enabled = settings.learning.enabled
        self._learning_review_after_turn = settings.learning.review_after_turn
        self._learning_packet_max_chars = settings.learning.packet_max_chars
        self.agent_name = settings.agent.name
        self.admin_workspace = settings.notifications.admin_workspace
        self.command_matcher = CommandMatcher.from_values(
            settings.trigger_pattern, settings.commands.model_dump()
        )
        self.queue: GroupQueue = GroupQueue(
            QueuePolicy(
                max_concurrent=settings.container.max_concurrent,
                max_retries=settings.queue.max_retries,
                retry_base_seconds=settings.queue.base_retry_seconds,
            ),
            ContainerRuntimeOperations(
                write_message=write_ipc_message,
                write_close_sentinel=write_ipc_close_sentinel,
                clean_input_dir=clean_ipc_input_dir,
                destroy_gate=destroy_gate,
                destroy_session=destroy_session,
                destroy_all_sessions=destroy_all_sessions,
                graceful_stop=graceful_stop,
            ),
        )
        self.container_agent_operations = agent_runner.ContainerAgentOperations(
            get_session=get_session,
            fresh_container_name=_fresh_container_name,
            spawn=_spawn_container,
            create_session=create_session,
            destroy_session=destroy_session,
            ensure_workspace_mcp=_ensure_workspace_mcp,
            wait_for_query=_wait_for_agent_query,
        )

        def prepare_host_codex_home(folder: str, plugin_manager: object | None) -> Path:
            return prepare_agent_homes(
                folder,
                cast("pluggy.PluginManager | None", plugin_manager),
            ).codex_home

        def host_learning_vault(folder: str) -> Path | None:
            paths = resolve_learning_paths(folder)
            return paths.vault_root if paths is not None else None

        def resolve_routed_host_cwd(
            group_folder: str,
            source_cwd: Path,
            repo_accesses: Sequence[str],
            *,
            recovered: bool,
        ) -> host_execution.HostExecutionCwd:
            try:
                repo_contexts = tuple(
                    repo_ctx
                    for slug in repo_accesses
                    if (repo_ctx := get_repo_context(slug)) is not None
                )
            except ValueError as exc:
                raise host_execution.HostExecutionCwdError(
                    "Routed host turn selected malformed repository access."
                ) from exc
            try:
                resolved = resolve_routed_host_worktree_cwd(
                    group_folder,
                    source_cwd,
                    repo_contexts,
                    recovered=recovered,
                )
            except RoutedHostWorktreeError as exc:
                raise host_execution.HostExecutionCwdError(str(exc)) from exc
            return host_execution.HostExecutionCwd(
                resolved.cwd,
                resolved.notices,
                resolved.repo_access,
            )

        self.host_runtime_operations = host_execution.HostRuntimeOperations(
            build_agent_environment=build_agent_env_vars,
            prepare_mcp=_prepare_host_direct_mcp_servers,
            sessions_root=settings.data_dir / "sessions",
            project_root=settings.project_root,
            gateway_port=settings.gateway.port,
            prepare_host_codex_home=prepare_host_codex_home,
            host_learning_vault=host_learning_vault,
            resolve_routed_host_cwd=resolve_routed_host_cwd,
        )
        self.ask_user_runtime_operations = AskUserRuntimeOperations(
            has_live_session=_has_live_container_session,
            persist_skill_access=_persist_skill_access_choice,
            write_response=_write_ask_user_response,
        )
        self.approval_runtime_operations = ApprovalRuntimeOperations(
            find_pending_by_short_id=find_pending_by_short_id,
            list_pending_approvals=list_pending_approvals,
            persist_and_process=partial(_persist_and_process_approval, self),
        )
        self.agent_execution_runtime = _agent_execution_runtime_config(settings)
        self.queue.set_process_messages_fn(
            lambda jid: message_handler.process_group_messages(self, jid)
        )
        self.channels: list[Channel] = []
        self.event_bus: EventBus = EventBus()
        self._shutting_down: bool = False
        self._http_runner: object | None = None
        self._observers: list[ObserverProvider] = []
        self._speech_synthesizer: SpeechSynthesizer | None = None
        self.subsystem_tasks = RuntimeTaskOwner()
        self.startup_readiness = StartupReadiness()
        self.connection_runtime_owner = ConnectionRuntimeOwner()
        self.plugin_manager: pluggy.PluginManager | None = None

    async def apply_config_candidate(
        self,
        candidate: object,
        *,
        affected_workspaces: tuple[str, ...],
        reconcile_automations: bool,
    ) -> None:
        """Publish a validated candidate and retire only affected sessions."""
        settings = cast("Settings", candidate)
        await self.startup_readiness.wait()
        targets = tuple(
            RuntimeTarget.from_workspace(workspace)
            for folder in affected_workspaces
            for workspace in self.workspaces.values()
            if workspace.folder == folder
        )
        runtime_ids = tuple(target.id for target in targets)
        if targets:
            await self.queue.pause_runtime_policy(targets)
        published = get_settings()
        previous_profiles = tuple(
            workspace
            for workspace in self.workspaces.values()
            if workspace.folder in affected_workspaces
        )
        updated_profiles = self._runtime_policy_profiles(settings, previous_profiles)
        candidate_published = False
        try:
            scheduler_runtime = await self._prepare_config_candidate(
                settings,
                reconcile_automations=reconcile_automations,
            )
            await self._publish_config_snapshot(
                settings,
                scheduler_runtime,
                updated_profiles,
                previous_profiles,
                published,
            )
            candidate_published = True
            await self._retire_runtime_policy_sessions(updated_profiles)
        except BaseException:
            if candidate_published:
                await self._rollback_config_snapshot(published, previous_profiles)
            raise
        finally:
            if runtime_ids:
                self.queue.resume_runtime_policy(runtime_ids)

    async def _prepare_config_candidate(
        self,
        settings: Settings,
        *,
        reconcile_automations: bool,
    ) -> SchedulerRuntimeConfig:
        scheduler_runtime = _scheduler_runtime_config(settings)
        if reconcile_automations:
            await reconcile_automation_jobs(self.workspaces, settings)
            await temporal_scheduler.reconcile_schedules_with_config(scheduler_runtime)
        return scheduler_runtime

    async def _publish_config_snapshot(
        self,
        settings: Settings,
        scheduler_runtime: SchedulerRuntimeConfig,
        updated_profiles: tuple[WorkspaceProfile, ...],
        previous_profiles: tuple[WorkspaceProfile, ...],
        previous_settings: Settings,
    ) -> None:
        if updated_profiles:
            await set_workspace_profiles(updated_profiles)
        try:
            publish_settings(settings)
            self._replace_workspace_profiles(updated_profiles)
            self._publish_live_runtime(settings, scheduler_runtime)
        except BaseException:
            publish_settings(previous_settings)
            self._replace_workspace_profiles(previous_profiles)
            self._publish_live_runtime(
                previous_settings,
                _scheduler_runtime_config(previous_settings),
            )
            if updated_profiles:
                await set_workspace_profiles(previous_profiles)
            raise

    async def _rollback_config_snapshot(
        self,
        settings: Settings,
        profiles: tuple[WorkspaceProfile, ...],
    ) -> None:
        publish_settings(settings)
        self._publish_live_runtime(settings, _scheduler_runtime_config(settings))
        if profiles:
            await set_workspace_profiles(profiles)
            self._replace_workspace_profiles(profiles)

    def _runtime_policy_profiles(
        self,
        settings: Settings,
        profiles: tuple[WorkspaceProfile, ...],
    ) -> tuple[WorkspaceProfile, ...]:
        updated = []
        for profile in profiles:
            config = settings.workspace_config(static_workspace_folder(profile.folder))
            resolved = load_resolved_config(profile.folder, settings=settings)
            if config is None or resolved is None:
                updated.append(profile)
                continue
            updated.append(
                replace(
                    profile,
                    security=workspace_security(config, resolved),
                )
            )
        return tuple(updated)

    def _publish_live_runtime(
        self,
        settings: Settings,
        scheduler_runtime: SchedulerRuntimeConfig,
    ) -> None:
        self.agent_execution_runtime = _agent_execution_runtime_config(settings)
        self.scheduler_runtime = scheduler_runtime
        self._learning_review_enabled = settings.learning.enabled
        self._learning_review_after_turn = settings.learning.review_after_turn
        self._learning_packet_max_chars = settings.learning.packet_max_chars
        _configure_learning_runtime(settings)
        temporal_scheduler.publish_scheduler_config(scheduler_runtime)

    async def _retire_runtime_policy_sessions(
        self,
        profiles: tuple[WorkspaceProfile, ...],
    ) -> None:
        for profile in profiles:
            await prepare_context_reset(self.plugin_manager, profile)
            await self.queue.destroy_runtime_session(RuntimeId(profile.folder))
        references = tuple(
            (
                GroupFolder(profile.folder),
                SessionId(session_id),
                ChatJid(profile.jid),
            )
            for profile in profiles
            if (session_id := self.sessions.get(profile.folder)) is not None
        )
        if references:
            await clear_runtime_session_references_batch(references)
        for profile in profiles:
            self.sessions.pop(profile.folder, None)
            self.session_cleared.add(profile.folder)
        self._replace_workspace_profiles(profiles)

    def _replace_workspace_profiles(
        self,
        profiles: tuple[WorkspaceProfile, ...],
    ) -> None:
        for profile in profiles:
            self.workspaces[profile.jid] = profile

    def _configure_runtime_dependencies(self, settings: Settings) -> None:
        """Wire concrete host adapters into subsystem-local runtime seams."""
        configure_config_refresh_runtime(
            ConfigRefreshRuntime(
                project_root=settings.project_root,
                apply_candidate=self.apply_config_candidate,
                automation_projection=cast(
                    "Callable[[object], object]",
                    automation_projection,
                ),
                configuration_source_digest=configuration_source_digest,
                get_settings=get_settings,
                load_runtime_candidate=load_runtime_candidate,
                restart_fingerprint=cast("Callable[[object], str]", restart_fingerprint),
                runtime_policy_changes=cast(
                    "Callable[[object, object, tuple[str, ...]], Any]",
                    runtime_policy_changes,
                ),
                workspace_folders=lambda: tuple(
                    sorted({workspace.folder for workspace in self.workspaces.values()})
                ),
            )
        )
        configure_ipc_base_dir(settings.data_dir / "ipc")
        configure_approval_state_root(settings.data_dir / "approvals")
        configure_security_audit_storage(
            store_security_audit=store_message_direct,
            prune_security_audit=prune_messages_by_sender,
        )
        configure_canary_runtime(
            CanaryRuntime(
                record_run=record_canary_run,
                latest_runs=get_latest_canary_runs,
                recent_runs=lambda limit: get_recent_canary_runs(limit=limit),
                unresolved_regressions=get_unresolved_canary_regressions,
                code_revision=lambda: get_head_sha() or "unknown",
            )
        )
        pending_questions.configure_pending_questions_ipc_base_dir(settings.data_dir / "ipc")
        configure_personalized_skills_root(settings.project_root)
        configure_git_default_cwd(settings.project_root)
        configure_git_sync_runtime(
            GitSyncRuntime(
                project_root=settings.project_root,
                repo_slugs=tuple(settings.repos.overrides),
                get_restart_hash=lambda: restart_fingerprint(load_runtime_candidate()),
            )
        )
        configure_deploy_git_runtime(
            DeployGitRuntime(
                get_head_sha=get_head_sha,
                get_deploy_config_hash=get_deploy_config_hash,
                run_git=run_git,
            )
        )
        configure_worktree_startup_runtime(
            WorktreeStartupRuntime(
                home_dir=settings.home_dir,
                project_root=settings.project_root,
                configured_tokens={
                    slug: (repo.token.get_secret_value() if repo.token is not None else None)
                    for slug, repo in settings.repos.overrides.items()
                },
            )
        )
        configure_allowed_message_filter(access.filter_allowed_messages)

        _configure_learning_runtime(settings)

        def workspace_skill_selection(
            folder: str,
        ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
            resolved = load_resolved_config(folder, settings=get_settings())
            if resolved is None:
                return None
            return (
                tuple(resolved.skills),
                tuple(resolved.denied_skills),
                tuple(resolved.tools),
            )

        configure_skill_activation_runtime(
            SkillActivationRuntime(
                project_root=settings.project_root,
                sessions_root=settings.data_dir / "sessions",
                tool_skills={
                    name: tuple(getattr(tool, "skills", ()))
                    for name, tool in settings.tools.items()
                },
                resolve_workspace_skill_selection=workspace_skill_selection,
                resolve_learning_paths=lambda folder, profile: resolve_learning_paths(
                    folder, profile_override=profile
                ),
            )
        )

        def missing_workspace_profile(
            folder: str, control_parent: WorkspaceProfile
        ) -> WorkspaceProfile | None:
            config = cast("WorkspaceConfig", settings.workspace_config(folder))
            resolved = cast("Any", settings.resolved_workspace_config(folder))
            return WorkspaceProfile(
                jid=control_parent.jid,
                name=folder.replace("-", " ").title(),
                folder=folder,
                trigger=control_parent.trigger,
                container_config=control_parent.container_config,
                security=workspace_security(config, resolved),
                is_admin=resolved.is_admin,
                added_at=datetime.now(UTC).isoformat(),
            )

        configure_workspace_placement(
            workspace_parent=settings.workspace_parent,
            missing_workspace_profile=missing_workspace_profile,
        )
        configure_runtime_override(settings.container.runtime)
        runtime = get_runtime()

        configure_mount_operations(
            MountOperations(
                prepare_agent_homes=_mount_agent_homes,
                repo_container_path=repo_container_path,
                runtime_name=lambda: runtime.name,
            )
        )
        configure_workspace_environment(
            lambda *, is_admin, group_folder: _workspace_environment(
                settings,
                is_admin=is_admin,
                group_folder=group_folder,
            )
        )
        configure_container_spawn_runtime(
            container_cli=runtime.cli,
            ensure_agent_image=ensure_agent_image_available,
            resolve_repo_mounts=_resolve_repo_mounts,
        )
        configure_container_process_runtime(
            container_cli=runtime.cli,
            is_apple_runtime=runtime.name == "apple",
            container_is_running=lambda name: name in runtime.list_running_containers(prefix=name),
        )
        _configure_container_policy_runtime(is_apple_container=runtime.name == "apple")
        configure_cop_gateway(
            model=settings.security.cop_model or settings.agent.model,
            wire_api=settings.security.cop_wire_api,
        )
        configure_cop_prompt_provider(_read_current_prompt)

    def is_shutting_down(self) -> bool:
        """Return whether shutdown has started."""
        return self._shutting_down

    def get_local_head_sha(self, project_root: Path) -> str:
        """Read the checkout revision for an explicit update-offer dependency."""
        return get_local_head_sha(project_root)

    def get_deploy_config_hash(self) -> str:
        """Read the restart-relevant configuration hash for update offers."""
        return get_deploy_config_hash()

    def current_deploy_revision(self) -> tuple[str, str]:
        """Read the current revision and its restart-relevant configuration hash."""
        return get_head_sha(), get_deploy_config_hash()

    def host_update_main(self, project_root: Path) -> bool:
        """Fetch the approved revision through the Git adapter."""
        return host_update_main(project_root)

    def needs_deploy(self, old_sha: str, new_sha: str) -> bool:
        """Determine whether an approved update requires a service restart."""
        return needs_deploy(old_sha, new_sha)

    def needs_container_rebuild(self, old_sha: str, new_sha: str) -> bool:
        """Determine whether an approved update changes the agent image."""
        return needs_container_rebuild(old_sha, new_sha)

    def refresh_personalized_agent_skills(self, group_folder: str) -> None:
        """Refresh learned skills before the next warm agent turn."""
        refresh_personalized_agent_skills(group_folder)

    def begin_shutdown(self) -> bool:
        """Mark shutdown as started; return False if shutdown was already active."""
        if self._shutting_down:
            return False
        self._shutting_down = True
        return True

    def require_plugin_manager(self, phase: str) -> pluggy.PluginManager:
        """Return composed plugins or fail with the startup phase that raced."""
        if self.plugin_manager is None:
            raise RuntimeError(f"phase 1 (_initialize_core) must run before {phase}")
        return self.plugin_manager

    def sync_personalization(self, project_root: Path) -> str:
        """Persist valid changes through the configured Git adapter."""
        with self._personalization_sync_lock:
            return self._sync_personalization_unlocked(project_root)

    def _sync_personalization_unlocked(self, project_root: Path) -> str:
        from pynchy.config.api import (  # noqa: PLC0415 - composition root selects the validator.
            validate_personalization_configuration,
        )
        from pynchy.host.git_ops.api import (  # noqa: PLC0415 - composition root selects the Git adapter.
            sync_personalization_repo,
        )

        return sync_personalization_repo(project_root, validate_personalization_configuration)

    def persist_capability_approval(self, group_folder: str, capability_id: str) -> None:
        """Publish a permanent capability grant before reporting success."""
        from pynchy.host.orchestrator import (  # noqa: PLC0415 - avoids app composition cycle.
            workspace_config,
        )

        with self._personalization_sync_lock:
            settings = get_settings()
            preparation = self._sync_personalization_unlocked(settings.project_root)
            if preparation not in {"idle", "pushed", "updated"}:
                raise ValueError("Could not prepare personalization repository for publication")
            reset_settings()
            workspace_config.update_workspace_capability_policy(
                group_folder,
                capability_id,
                publish=self._sync_personalization_unlocked,
            )

    async def request_mcp_approval(self, request: McpApprovalRequest) -> None:
        """Present a proxy-gated MCP call through the shared approval state machine."""
        chat_jid = next(
            (
                jid
                for jid, workspace in self.workspaces.items()
                if workspace.folder == request.group_folder
            ),
            None,
        )
        if chat_jid is None:
            raise ValueError(f"No chat is registered for workspace {request.group_folder}")
        control = await get_conversation_control_by_thread(ChatJid(chat_jid))
        gate = get_gate_for_group(request.group_folder)
        short_id = create_pending_approval(
            request_id=request.request_id,
            tool_name=request.tool_name,
            source_group=request.group_folder,
            approval_chat_jid=chat_jid,
            request_data=request.request_data,
            handler_type="mcp_proxy",
            capability_id=request.capability_id,
            origin_conversation_id=(str(control.conversation_id) if control is not None else None),
            corruption_tainted=bool(gate and gate.policy.corruption_tainted),
            secret_tainted=bool(gate and gate.policy.secret_tainted),
        )
        await self.broadcast_to_channels(
            chat_jid,
            approval_event(
                request.tool_name,
                request.request_data,
                short_id,
                capability_id=request.capability_id,
            ),
        )
        await record_security_event(
            chat_jid=chat_jid,
            workspace=request.group_folder,
            tool_name=request.tool_name,
            decision="approval_requested",
            corruption_tainted=bool(gate and gate.policy.corruption_tainted),
            secret_tainted=bool(gate and gate.policy.secret_tainted),
            reason=request.reason,
            request_id=request.request_id,
            capability_id=request.capability_id,
        )

    async def bind_routed_session(self, group_folder: str, session_id: SessionId) -> None:
        """Attach a conversation-owned session to its current runtime placement."""
        if group_folder in self.session_cleared:
            return
        self.sessions[group_folder] = session_id
        await set_session(GroupFolder(group_folder), session_id)

    async def cleanup_http_runner(self) -> None:
        runner = self._http_runner
        if runner is None:
            return
        self._http_runner = None
        await cast("Any", runner).cleanup()

    def set_http_runner(self, runner: object) -> None:
        self._http_runner = runner

    def attach_observers(self, observers: list[ObserverProvider]) -> None:
        self._observers = observers

    async def close_observers(self) -> None:
        for observer in self._observers:
            await observer.close()

    def set_speech_synthesizer(self, speech_synthesizer: SpeechSynthesizer | None) -> None:
        """Set the host-side provider used for spoken channel replies."""
        self._speech_synthesizer = speech_synthesizer

    def get_speech_synthesizer(self) -> SpeechSynthesizer | None:
        """Return the host-side provider used for spoken channel replies."""
        return self._speech_synthesizer

    def routing_cursor(self, chat_jid: str) -> str:
        """Return the cursor for fetching messages during routing."""
        return monotonic_cursor(
            self.last_agent_timestamp.get(chat_jid, ""),
            self._dispatched_through.get(chat_jid, ""),
        )

    def mark_dispatched(self, chat_jid: str, timestamp: str) -> None:
        """Record the furthest message timestamp dispatched to an active container."""
        self._dispatched_through[chat_jid] = monotonic_cursor(
            self._dispatched_through.get(chat_jid, ""),
            timestamp,
        )

    def pop_dispatched(self, chat_jid: str, default: str) -> str:
        """Return and clear the in-memory dispatched timestamp for a chat."""
        return self._dispatched_through.pop(chat_jid, default)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    async def load_state(self) -> None:
        """Load persisted state from the database."""
        self.last_timestamp = await get_router_state("last_timestamp") or ""
        agent_ts = await get_router_state("last_agent_timestamp")
        try:
            self.last_agent_timestamp = json.loads(agent_ts) if agent_ts else {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupted last_agent_timestamp in DB, resetting")
            self.last_agent_timestamp = {}
        self.sessions = await get_all_sessions()

        self.workspaces = await get_all_workspace_profiles()
        workspace_jids = list(self.workspaces)
        self.last_timestamp = await upgrade_message_cursor(workspace_jids, self.last_timestamp)
        for chat_jid, cursor in self.last_agent_timestamp.items():
            self.last_agent_timestamp[chat_jid] = await upgrade_message_cursor([chat_jid], cursor)

        logger.info(
            "State loaded",
            workspace_count=len(self.workspaces),
        )

    async def save_state(self) -> None:
        """Persist router state to the database atomically.

        Both rows are written in a single transaction so a crash can never
        leave them inconsistent.
        """
        await save_router_state_batch(
            {
                "last_timestamp": self.last_timestamp,
                "last_agent_timestamp": json.dumps(self.last_agent_timestamp),
            }
        )

    # ------------------------------------------------------------------
    # Protocol adapter methods (satisfy handler Protocols via structural typing)
    # ------------------------------------------------------------------

    async def handle_context_reset(
        self,
        chat_jid: str,
        group: WorkspaceProfile,
        timestamp: str,
        *,
        source_message: NewMessage | None = None,
    ) -> None:
        await session_handler.handle_context_reset(
            self, chat_jid, group, timestamp, source_message=source_message
        )

    async def prepare_context_reset(self, group: WorkspaceProfile) -> None:
        """Await plugin-owned teardown before clearing a session."""
        await prepare_context_reset(self.plugin_manager, group)

    async def reset_scheduled_context(
        self,
        task: ScheduledTask,
        group: WorkspaceProfile,
        occurrence_id: str,
    ) -> None:
        await session_handler.handle_scheduled_context_reset(self, task.id, group, occurrence_id)

    async def handle_end_session(
        self,
        chat_jid: str,
        group: WorkspaceProfile,
        timestamp: str,
        *,
        source_message: NewMessage | None = None,
    ) -> None:
        await session_handler.handle_end_session(
            self, chat_jid, group, timestamp, source_message=source_message
        )

    async def trigger_manual_redeploy(
        self,
        chat_jid: str,
        *,
        source_message: NewMessage | None = None,
    ) -> None:
        await session_handler.trigger_manual_redeploy(self, chat_jid, source_message=source_message)

    async def catch_up_channels(self) -> None:
        await self._catch_up_channel_history()

    async def start_channel_reconciliation(self) -> None:
        """Start durable Temporal reconciliation for channel history."""
        await temporal_scheduler.start_channel_reconciliation_workflow()

    async def start_linear_work_item_reconciliation(self) -> None:
        """Start durable Temporal reconciliation for managed Linear work."""
        await temporal_scheduler.start_linear_work_item_reconciliation_workflow()

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict[str, Any]], *, source: str = "user"
    ) -> None:
        await output_handler.broadcast_agent_input(self, chat_jid, messages, source=source)

    run_agent = agent_runner.run_agent
    automation_memory_dir = staticmethod(automation_memory_dir)

    async def review_linear_plan(
        self,
        request: LinearPlanReviewRequest,
    ) -> LinearPlanReviewResult:
        """Run one plan review with the current configured reviewer prompt."""
        return await linear_plan_review.review_linear_plan(
            self,
            request,
            _read_current_prompt("plan_freshness"),
        )

    def emit(self, event: Event) -> None:
        self.event_bus.emit(event)

    async def broadcast_to_channels(
        self, chat_jid: str, event: OutboundEvent, *, suppress_errors: bool = True
    ) -> None:
        await broadcast(self, chat_jid, event, suppress_errors=suppress_errors)

    async def send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None:
        await channel_handler.send_reaction_to_channels(self, chat_jid, message_id, sender, emoji)

    def filter_allowed_messages(
        self,
        messages: list[NewMessage],
        group: WorkspaceProfile,
        channel_plugin_name: str | None,
    ) -> list[NewMessage]:
        return access.filter_allowed_messages(messages, group, channel_plugin_name)

    def linear_workspace_enabled(self, group: WorkspaceProfile) -> bool:
        return linear_workspace_enabled(group)

    async def create_linear_workspace_todo(
        self, group: WorkspaceProfile, title: str
    ) -> dict[str, object] | None:
        return await create_linear_workspace_todo(group, title)

    def processing_ack_emoji(self, chat_jid: str) -> str | None:
        return channel_handler.processing_ack_emoji(self, chat_jid)

    async def send_reaction_to_outbound(
        self, chat_jid: str, per_channel_ids: dict[str, str], emoji: str
    ) -> None:
        await channel_handler.send_reaction_to_outbound(self, chat_jid, per_channel_ids, emoji)

    async def set_typing_on_channels(self, chat_jid: str, *, is_typing: bool) -> None:
        await channel_handler.set_typing_on_channels(self, chat_jid, is_typing=is_typing)

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        await send_host_message(self, chat_jid, text)

    def direct_command_workdir(self, group: WorkspaceProfile) -> Path:
        """Resolve a direct command workspace from host-owned configuration."""
        return get_settings().groups_dir / group.folder

    async def record_direct_command_output(self, output: DirectCommandOutput) -> None:
        """Persist one direct command result through the state adapter."""
        await store_message_direct(
            message_id=f"command-output-{output.source_message.id}",
            chat_jid=output.chat_jid,
            sender="command_output",
            sender_name="command",
            content=output.content,
            timestamp=output.timestamp,
            is_from_me=True,
            message_type="host",
            metadata={
                "source": "direct_command",
                "command": output.command,
                "exit_code": output.exit_code,
                "source_message_id": output.source_message.id,
                "workspace_name": output.group.name,
                "workspace_folder": output.group.folder,
            },
        )

    async def scheduled_execution_lifecycle(
        self, task_id: str
    ) -> ScheduledExecutionLifecycle | None:
        """Adapt the durable work-item record to scheduled completion policy."""
        execution = await get_work_item_execution_for_task(task_id)
        if execution is None:
            return None
        return ScheduledExecutionLifecycle(
            execution_id=execution.id,
            status=execution.status.value,
            has_explicit_outcome=execution.status.is_explicit_lifecycle_outcome,
        )

    async def get_scheduled_conversation(
        self, conversation_id: ConversationId
    ) -> Conversation | None:
        """Load the durable conversation needed to bind a scheduled task."""
        return await get_conversation(conversation_id)

    async def persist_scheduled_task_updates(
        self, task_id: str, updates: dict[str, object]
    ) -> None:
        """Persist the binding use case's changed scheduled-task fields."""
        await update_task(task_id, updates)

    async def cancel_scheduled_task(self, task_id: str) -> None:
        """Retire scheduled work rejected by its terminal conversation control."""
        await cancel_task_and_checkpoint(task_id)

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None:
        await send_system_notice(self, chat_jid, text)

    async def run_declared_canaries(
        self, target_profile: str, scenario_ids: tuple[str, ...]
    ) -> list[CanaryRun]:
        """Run configured canaries through the concrete adapter."""
        return await run_declared_canaries(
            target_profile=target_profile,
            scenario_ids=scenario_ids,
            scheduler_deps=self,
        )

    async def run_learning_review(self, packet: LearningPacket) -> str:
        """Run a hidden learning review through the workspace-owned queue."""

        async def run_agent_via_queue(  # noqa: PLR0913 - callback mirrors the agent runner.
            group: WorkspaceProfile,
            chat_jid: str,
            messages: list[dict[str, Any]],
            on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
            extra_system_notices: list[str] | None = None,
            *,
            is_scheduled_task: bool = False,
            repo_access_override: str | None = None,
            input_source: str = "user",
        ) -> str:
            loop = asyncio.get_running_loop()
            result_future: asyncio.Future[str] = loop.create_future()

            async def run_queued_agent() -> None:
                if result_future.cancelled():
                    return
                try:
                    result = await self.run_agent(
                        group,
                        chat_jid,
                        messages,
                        on_output=on_output,
                        extra_system_notices=extra_system_notices,
                        is_scheduled_task=is_scheduled_task,
                        repo_access_override=repo_access_override,
                        input_source=input_source,
                    )
                except asyncio.CancelledError:
                    if not result_future.done():
                        result_future.cancel()
                    raise
                except Exception as exc:  # noqa: BLE001 - allow: exception-handling; propagate queued run failure.
                    logger.exception("Queued learning run failed", err=str(exc))
                    if not result_future.done():
                        result_future.set_exception(exc)
                else:
                    if not result_future.done():
                        result_future.set_result(result)

            accepted = self.queue.enqueue_task(
                RuntimeTarget.from_workspace(group),
                f"learning-review-{uuid.uuid4().hex}",
                run_queued_agent,
            )
            if accepted is False:
                result_future.cancel()
                raise asyncio.CancelledError
            try:
                return await result_future
            except asyncio.CancelledError:
                result_future.cancel()
                raise

        return await run_host_learning_review(
            packet,
            run_agent_via_queue,
            _read_current_prompt("learning"),
        )

    async def reconcile_linear_work_items(self) -> int | None:
        """Reconcile managed Linear work when at least one board is configured."""
        boards = linear_workspace_boards()
        if not boards:
            return None
        admitted = await reconcile_all_linear_work_items(
            self.workspaces,
            boards,
            review_plan=self.review_linear_plan,
            broadcast_host_message=self.broadcast_host_message,
            defer_plan_review=temporal_scheduler.start_linear_plan_review_workflow,
        )
        return len(admitted)

    async def ensure_linear_issue_control(self, control: LinearIssueControl) -> None:
        """Project one active Linear issue into its silent forum control."""
        conversation = await resolve_linear_issue_conversation(
            control.issue_id,
            control.workspace,
            control.account_name,
        )
        await linear_issue_controls.ensure_issue_control(self, control, conversation)

    async def process_linear_plan_review_admission(
        self,
        admission: LinearPlanReviewAdmission,
        *,
        attempt: int = 1,
        reset_context: Callable[[str], Awaitable[None]] | None = None,
    ) -> bool:
        """Review one exact issue revision and start admitted execution immediately."""
        task = await admit_linear_plan_review(
            admission,
            self.workspaces.values(),
            review_plan=self.review_linear_plan,
            broadcast_host_message=self.broadcast_host_message,
            attempt=attempt,
            reset_context=reset_context or self.reset_linear_plan_review_context,
        )
        if task is None:
            return False
        await temporal_scheduler.start_scheduled_agent_task_workflow(task)
        return True

    async def reset_linear_plan_review_context(self, chat_jid: str) -> None:
        """Run the normal context reset for a plan-review issue thread."""
        group = self.workspaces.get(chat_jid)
        if group is None:
            logger.warning("Linear plan review reset workspace is unavailable", chat_jid=chat_jid)
            return
        timestamp = datetime.now(UTC).isoformat()
        await self.handle_context_reset(
            chat_jid,
            group,
            timestamp,
            source_message=NewMessage(
                id=f"linear-plan-review-reset-{uuid.uuid4().hex[:8]}",
                chat_jid=chat_jid,
                sender="system",
                sender_name="System",
                content="/reset",
                timestamp=timestamp,
                is_from_me=False,
                message_type="host",
            ),
        )

    async def handle_streamed_output(
        self,
        chat_jid: str,
        group: WorkspaceProfile,
        result: ContainerOutput,
        *,
        turn_id: str | None = None,
    ) -> bool:
        return await output_handler.handle_streamed_output(
            self, chat_jid, group, result, turn_id=turn_id
        )

    # ------------------------------------------------------------------
    # Group management
    # ------------------------------------------------------------------

    async def _register_workspace(self, profile: WorkspaceProfile) -> None:
        """Register a workspace and persist it."""
        await set_workspace_profile(profile)
        self.workspaces[profile.jid] = profile

        workspace_dir = get_settings().groups_dir / profile.folder
        (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

        logger.info(
            "Workspace registered",
            jid=profile.jid,
            name=profile.name,
            folder=profile.folder,
        )

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        """Register a workspace from subsystem adapters."""
        await self._register_workspace(profile)

    async def rebind_workspace(self, profile: WorkspaceProfile) -> None:
        await rebind_workspace_runtime(profile, self.workspaces, self.queue)

    async def unregister_workspace(self, jid: str) -> None:
        """Remove an orphaned workspace registration."""
        self.workspaces.pop(jid, None)
        await delete_workspace_profile(jid)

    async def retire_workspace_runtime(self, folder: str) -> None:
        """Stop one retired workspace and reclaim its safe filesystem artifacts."""
        runtime_id = RuntimeId(folder)
        self.queue.clear_pending_tasks(runtime_id)
        self.queue.clear_pending_messages(runtime_id)
        await self.queue.stop_active_process_for_control(runtime_id)
        self.queue.clear_pending_messages(runtime_id)
        await destroy_session(folder)
        self.sessions.pop(folder, None)
        self.session_cleared.add(folder)
        await clear_session(GroupFolder(folder))
        settings = get_settings()
        await asyncio.to_thread(
            cleanup_workspace_artifacts,
            folder,
            data_dir=settings.data_dir,
            groups_dir=settings.groups_dir,
            worktrees_dir=settings.worktrees_dir,
            git=run_git,
        )

    async def reclaim_orphaned_workspace_artifacts(
        self,
        tasks: Sequence[ScheduledTask],
    ) -> None:
        """Reclaim dynamic workspace artifacts with no durable runtime owner."""
        settings = get_settings()
        await cleanup_startup_workspace_artifacts(
            self.workspaces.values(),
            tasks,
            self.active_worktree_folders(),
            data_dir=settings.data_dir,
            groups_dir=settings.groups_dir,
            worktrees_dir=settings.worktrees_dir,
            git=run_git,
        )

    async def get_available_groups(self) -> list[dict[str, Any]]:
        """Get available groups list for the agent, ordered by most recent activity."""
        return available_workspace_groups(
            await get_all_chats(),
            self.workspaces,
            self.channels,
        )

    def admin_repo_notices(
        self, group_folder: str, *, is_admin: bool, repo_access: str | None
    ) -> list[str]:
        """Resolve source-control state for the pre-container admin notice."""
        repo_context = get_repo_context(repo_access) if repo_access else None
        cwd = repo_context.worktrees_dir / group_folder if repo_context else None
        return agent_runner.build_admin_system_notices(
            is_admin=is_admin,
            repo_dirty=is_repo_dirty(cwd=cwd),
            unpushed_commits=count_unpushed_commits(cwd=cwd),
        )

    def repo_is_dirty(self) -> bool:
        """Return the host checkout's source-control cleanliness."""
        return is_repo_dirty()

    def active_worktree_folders(self) -> set[str]:
        """Return worktree folders protected by running execution or a live session."""
        return self.queue.active_folders() | active_session_group_folders()

    def new_learning_run_summary(self) -> learning_capture.LearningRunSummary:
        """Create the per-turn evidence buffer for best-effort learning."""
        return learning_capture.LearningRunSummary()

    def observe_learning_output(self, summary: object, output: ContainerOutput) -> None:
        """Keep best-effort learning observation at the composition boundary."""
        learning_capture.observe_learning_output(
            cast("learning_capture.LearningRunSummary", summary),
            output,
        )

    async def start_completed_turn_learning_review(
        self,
        chat_jid: str,
        group: WorkspaceProfile,
        messages: list[NewMessage],
        final_cursor: str,
        summary: object,
    ) -> None:
        """Capture completed-turn learning through the selected host adapter."""
        await learning_capture.start_completed_turn_learning_review(
            chat_jid,
            group,
            messages,
            final_cursor,
            cast("learning_capture.LearningRunSummary", summary),
            lambda jid, cursor: load_allowed_group_messages(
                self,
                jid,
                group,
                cursor,
            ),
            self.start_learning_review_workflow,
            enabled=self._learning_review_enabled,
            review_after_turn=self._learning_review_after_turn,
            packet_max_chars=self._learning_packet_max_chars,
        )

    # ------------------------------------------------------------------
    # Message processing delegation
    # ------------------------------------------------------------------

    async def process_group_messages(self, chat_jid: str) -> TurnOutcome:
        return await message_handler.run_queued_message_turn(self, chat_jid)

    async def start_interactive_turn(self, chat_jid: str) -> None:
        """Start durable Temporal processing for pending messages in one chat."""
        await temporal_scheduler.start_interactive_message_workflow(chat_jid)

    async def start_interrupted_turn(self, turn_id: str, group_folder: str) -> None:
        """Start durable semantic recovery for one interrupted agent turn."""
        await temporal_scheduler.start_interrupted_turn_workflow(turn_id, group_folder)

    async def start_learning_review_workflow(self, packet: LearningPacket) -> None:
        """Start durable review for a completed learning packet."""
        await temporal_scheduler.start_learning_review_workflow(packet)

    # ------------------------------------------------------------------
    async def ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None:
        await session_handler.ingest_user_message(self, msg, source_channel=source_channel)

    async def on_inbound(self, jid: str, msg: NewMessage) -> None:
        await session_handler.on_inbound(self, jid, msg)

    async def on_reaction(self, jid: str, message_ts: str, user_id: str, emoji: str) -> None:
        """Handle an inbound reaction from a channel."""
        await reaction_handler.handle_reaction(self, jid, message_ts, user_id, emoji)

    async def on_ask_user_answer(self, request_id: str, answer: dict[str, Any]) -> None:
        """Handle an ask_user answer from a channel interaction callback."""
        if await update_offer.handle_update_offer_answer(request_id, answer, self):
            return
        await ask_user_handler.handle_ask_user_answer(request_id, answer, self)

    def has_active_host_process(self, group_folder: str) -> bool:
        """Return whether a direct host agent is blocked on this group's IPC."""
        return self.queue.has_active_host_process(group_folder)

    def register_idle_callback(
        self, group_folder: GroupFolder, callback: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Attach an idle callback to a live container session when one exists."""
        if (session := get_session(group_folder)) is not None:
            session.set_idle_callback(callback)

    def has_api_credentials(self) -> bool:
        """Report whether the selected runtime can authenticate an agent turn."""
        return has_api_credentials()

    async def destroy_runtime_session(self, group_folder: str) -> None:
        """Discard the container session for one resettable workspace."""
        await self.queue.destroy_runtime_session(RuntimeId(group_folder))

    async def enqueue_message(self, chat_jid: str, text: str) -> None:
        """Inject a synthetic message for cold-start answer delivery.

        Satisfies the AskUserDeps protocol.  Stores the message directly
        and triggers queue processing, bypassing user-message filters
        (allowed_users, trigger patterns) that would reject system messages.

        The forwarded answer is treated as inbound conversation content so the
        next agent turn can pick it up from SQLite. The host message below
        ensures the user sees what was forwarded (token stream transparency).
        """
        msg = NewMessage(
            id=f"ask-user-answer-{uuid.uuid4().hex[:8]}",
            chat_jid=chat_jid,
            sender="system",
            sender_name="System",
            content=text,
            timestamp=datetime.now(UTC).isoformat(),
            is_from_me=False,
            message_type="system",
        )
        await store_message(msg, message_type=msg.message_type or "system")
        await self.broadcast_host_message(chat_jid, "\U0001f60e Answer forwarded to agent")
        await self.start_interactive_turn(chat_jid)

    async def send_clear_confirmation(self, chat_jid: str) -> None:
        await session_handler.send_clear_confirmation(self, chat_jid)

    # ------------------------------------------------------------------
    # Channel history catch-up
    # ------------------------------------------------------------------

    async def _catch_up_channel_history(self) -> None:
        """Start Temporal-owned channel history reconciliation."""
        if not temporal_scheduler.temporal_scheduler_runtime_active():
            logger.info("Channel reconciliation deferred until Temporal scheduler runtime starts")
            return
        try:
            await self.start_channel_reconciliation()
        except temporal_scheduler.TemporalRuntimeUnavailableError:
            logger.info("Channel reconciliation deferred until Temporal scheduler runtime starts")
        except Exception as exc:  # noqa: BLE001 - allow: exception-handling; history catch-up is best-effort startup work.
            logger.warning(
                "Channel reconciliation skipped after startup dispatch failure",
                exc_type=type(exc).__name__,
                err=str(exc),
            )

    # ------------------------------------------------------------------
    # Lifecycle (delegated to _lifecycle module)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point — see :func:`pynchy.host.orchestrator.lifecycle.run_app`."""
        from pynchy.host.orchestrator.lifecycle import (  # noqa: PLC0415 - lifecycle imports PynchyApp for runtime annotations.
            run_app,
        )

        await run_app(self)
