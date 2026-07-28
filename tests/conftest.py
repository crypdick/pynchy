"""Shared test fixtures for Pynchy."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, SecretStr

from pynchy.actions import ACTION_SPECS, ActionId, assess_hermetic_coverage
from pynchy.canaries.api import CanaryRuntime, configure_canary_runtime
from pynchy.config.api import (
    AgentConfig,
    CanaryConfig,
    CommandCenterConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    access,
    repository_settings_sources,
)
from pynchy.host.container_manager.api import AgentHomeMounts, RepoMountResolution
from pynchy.host.container_manager.gateway import configure_gateway_runtime
from pynchy.host.container_manager.ipc.write import configure_ipc_base_dir
from pynchy.host.container_manager.mounts import MountOperations, configure_mount_operations
from pynchy.host.container_manager.orchestrator import configure_container_spawn_runtime
from pynchy.host.container_manager.process import configure_container_process_runtime
from pynchy.host.container_manager.security.approval import configure_approval_state_root
from pynchy.host.container_manager.security.audit import configure_security_audit_storage
from pynchy.host.container_manager.security.cop import (
    CopContextAvailability,
    CopInspectionContext,
    CopVerdict,
)
from pynchy.host.git_ops.utils import configure_git_default_cwd
from pynchy.host.learning.api import (
    LearningPathsRuntime,
    configure_learning_paths_runtime,
    prepare_agent_homes,
    prepare_vault_mount_root,
    resolve_learning_paths,
)
from pynchy.host.learning.mirror import configure_vault_mount_mirror
from pynchy.host.learning.skill_activation import (
    SkillActivationRuntime,
    configure_skill_activation_runtime,
)
from pynchy.host.learning.skills import configure_personalized_skills_root
from pynchy.host.orchestrator.agent_runner import ContainerAgentOperations
from pynchy.host.orchestrator.api import (
    ContainerRuntimeOperations,
    execute_action_intent,
    policy_approval_timestamp,
    prepare_action_intent,
    static_workspace_folder,
)
from pynchy.host.orchestrator.host_execution import HostRuntimeOperations
from pynchy.host.orchestrator.messaging.deps import CommandMatcher
from pynchy.host.orchestrator.messaging.pending_questions import (
    configure_pending_questions_ipc_base_dir,
)
from pynchy.host.orchestrator.messaging.reconciler import configure_allowed_message_filter
from pynchy.host.orchestrator.workspace_placement import configure_workspace_placement
from pynchy.host.orchestrator.workspace_registration import workspace_security
from pynchy.plugins.api import (
    ApprovalContract,
    ApprovalMode,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    HostActionAccess,
    HostActionCatalog,
    HostActionDescriptor,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    InboundFetchResult,
    NewMessage,
)
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccountRuntime,
    configure_linear_account_runtime,
    linear_account,
    linear_account_for_workspace,
)
from pynchy.plugins.integrations.linear_boot import (
    LinearBootRuntime,
    configure_linear_boot_runtime,
    configured_linear_workspace_names,
)
from pynchy.plugins.integrations.linear_conversation_identity import (
    LinearConversationRuntime,
    configure_linear_conversation_runtime,
)
from pynchy.plugins.integrations.linear_legacy_work_items import (
    LinearLegacyWorkItemRuntime,
    configure_linear_legacy_work_item_runtime,
)
from pynchy.plugins.integrations.linear_planning_tasks import (
    LinearPlanningTaskRuntime,
    configure_linear_planning_task_runtime,
)
from pynchy.plugins.integrations.linear_self_echoes import (
    LinearSelfEchoRuntime,
    configure_linear_self_echo_runtime,
)
from pynchy.plugins.integrations.linear_webhook_config import LinearPluginOptions
from pynchy.plugins.integrations.linear_webhook_effects import (
    LinearWebhookEffectsRuntime,
    configure_linear_webhook_effects_runtime,
)
from pynchy.plugins.integrations.linear_webhooks import (
    LinearWebhookRuntime,
    configure_linear_webhook_runtime,
)
from pynchy.plugins.integrations.linear_work_item_completion import (
    LinearWorkItemCompletionRuntime,
    configure_linear_work_item_completion_runtime,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkItemRuntime,
    configure_linear_work_item_runtime,
)
from pynchy.plugins.integrations.linear_work_item_tasks import (
    LinearWorkItemTaskRuntime,
    configure_linear_work_item_task_runtime,
)
from pynchy.plugins.integrations.linear_work_items import (
    LinearWorkItemsRuntime,
    configure_linear_work_items_runtime,
)
from pynchy.state import (
    WorkItemClaimRequest,
    WorkItemTransitionRequest,
    WorkItemTransitionResolution,
    apply_conversation_control_state,
    approve_action_intent,
    begin_webhook_effect,
    begin_work_item_transition,
    begin_work_item_transition_if_lifecycle_current,
    bind_work_item_execution_to_task,
    bind_work_item_execution_to_turn,
    cancel_work_item_execution,
    cancel_work_item_execution_if_lifecycle_current,
    close_test_database,
    confirm_webhook_effect,
    conversation_control_state_matches,
    create_host_job,
    create_task,
    create_task_if_absent,
    create_work_item_claim,
    delete_host_job,
    delete_task,
    deny_action_intent,
    expire_action_intent,
    fail_action_intent,
    fail_webhook_effect,
    get_action_intent_by_request,
    get_active_work_item_execution,
    get_all_tasks,
    get_conversation_control_binding,
    get_conversation_control_by_thread,
    get_conversation_for_subject_key,
    get_host_job_by_id,
    get_in_flight_turn_for_group,
    get_latest_canary_runs,
    get_latest_unresolved_work_item_transition,
    get_recent_canary_runs,
    get_task_by_id,
    get_task_run_logs,
    get_unfinished_work_item_execution,
    get_unresolved_canary_regressions,
    get_work_item_execution,
    get_work_item_execution_for_issue,
    get_work_item_transition_by_request,
    init_test_database,
    list_work_item_executions,
    mark_action_intent_awaiting_approval,
    mark_webhook_effect_executing,
    mark_webhook_effect_outcome_unknown,
    prune_messages_by_sender,
    record_canary_run,
    resolve_conversation,
    resolve_work_item_transition,
    resolve_work_item_transition_if_lifecycle_current,
    resume_once_task_after_unclaimed_scheduled_turn,
    resume_task,
    store_message_direct,
    update_host_job,
    update_task,
)
from pynchy.workspace.api import WorkspaceProfile


def configure_workspace_placement_for(settings: Settings) -> None:
    """Wire workspace placement to one test's resolved settings."""

    def missing_workspace_profile(folder, control_parent):
        config = settings.workspace_config(folder)
        resolved = settings.resolved_workspace_config(folder)
        if config is None or resolved is None:
            return None
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


def configure_skill_activation_for(settings: Settings) -> None:
    """Wire skill activation to one test's resolved settings."""

    def workspace_skill_selection(folder: str):
        resolved = settings.resolved_workspace_config(folder)
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
                name: tuple(getattr(tool, "skills", ())) for name, tool in settings.tools.items()
            },
            resolve_workspace_skill_selection=workspace_skill_selection,
            resolve_learning_paths=lambda folder, profile: resolve_learning_paths(
                folder, profile_override=profile
            ),
        )
    )


def configure_learning_paths_for(settings: Settings) -> None:
    """Wire learning-path resolution to one test's resolved settings."""

    def profile_for_workspace(folder: str) -> str | None:
        workspace = settings.workspaces.get(folder)
        return workspace.profiles[0] if workspace is not None and workspace.profiles else None

    configure_learning_paths_runtime(
        LearningPathsRuntime(
            enabled=settings.learning.enabled,
            vault_root=settings.learning.obsidian.vault_root,
            vault_mount_path=settings.learning.obsidian.mount_path,
            default_profile_root=settings.learning.obsidian.default_profile_root,
            memory_dir_name=settings.learning.obsidian.memory_dir_name,
            data_dir=settings.data_dir,
            profile_for_workspace=profile_for_workspace,
        )
    )


def configure_linear_accounts_for(settings: Settings) -> None:
    """Wire Linear account lookups to one test's resolved settings."""

    def workspace_tool_names(workspace: str) -> tuple[str, ...] | None:
        resolved = settings.resolved_workspace_config(workspace)
        return tuple(resolved.tools) if resolved is not None else None

    configure_linear_boot_runtime(
        LinearBootRuntime(
            workspace_names=tuple(settings.workspace_names()),
            account_for_name=lambda name: linear_account(name, settings.tools),
            account_for_workspace=lambda workspace: linear_account_for_workspace(
                workspace,
                tools=settings.tools,
                workspace_tool_names=workspace_tool_names,
            ),
            workspace_parent=settings.workspace_parent,
            canonical_workspace_folder=static_workspace_folder,
            additional_workspaces=lambda _registered: (),
        )
    )

    plugin = settings.plugins.get("linear")
    configure_linear_webhook_runtime(
        LinearWebhookRuntime(
            options=LinearPluginOptions.model_validate(
                plugin.options if plugin is not None else {}
            ),
            account_for_name=lambda name: linear_account(name, settings.tools),
            workspace_tools=workspace_tool_names,
            workspace_names_for_account=configured_linear_workspace_names,
        )
    )

    configure_linear_account_runtime(
        LinearAccountRuntime(
            tools=settings.tools,
            workspace_tool_names=workspace_tool_names,
        )
    )
    configure_linear_conversation_runtime(
        LinearConversationRuntime(
            get_unfinished_execution=get_unfinished_work_item_execution,
            get_for_subject_key=lambda key, workspace, suffix: get_conversation_for_subject_key(
                key,
                workspace=workspace,
                namespace_suffix=suffix,
            ),
            resolve=resolve_conversation,
        )
    )
    configure_linear_webhook_effects_runtime(
        LinearWebhookEffectsRuntime(
            resolve_conversation=resolve_conversation,
            control_state_matches=conversation_control_state_matches,
            apply_control_state=apply_conversation_control_state,
            get_execution_for_issue=get_work_item_execution_for_issue,
            cancel_execution=cancel_work_item_execution,
            cancel_execution_if_lifecycle_current=cancel_work_item_execution_if_lifecycle_current,
            get_active_execution=get_active_work_item_execution,
        )
    )
    configure_linear_work_item_task_runtime(
        LinearWorkItemTaskRuntime(
            get_control_binding=get_conversation_control_binding,
            get_task=get_task_by_id,
            create_task=create_task_if_absent,
            update_task=update_task,
            get_task_logs=get_task_run_logs,
            bind_execution_to_task=bind_work_item_execution_to_task,
            get_active_execution=get_active_work_item_execution,
            resume_once_task=resume_once_task_after_unclaimed_scheduled_turn,
            get_execution_for_issue=get_work_item_execution_for_issue,
        )
    )
    configure_linear_work_items_runtime(
        LinearWorkItemsRuntime(
            list_executions=list_work_item_executions,
            get_active_execution=get_active_work_item_execution,
            get_execution_for_issue=get_work_item_execution_for_issue,
            get_in_flight_turn=get_in_flight_turn_for_group,
            bind_execution_to_turn=bind_work_item_execution_to_turn,
            get_latest_unresolved_transition=get_latest_unresolved_work_item_transition,
        )
    )
    configure_linear_work_item_runtime(
        LinearWorkItemRuntime(
            get_transition_by_request=get_work_item_transition_by_request,
            get_execution=get_work_item_execution,
            get_active_execution=get_active_work_item_execution,
            create_claim=create_work_item_claim,
            claim_request=WorkItemClaimRequest,
            begin_transition=begin_work_item_transition,
            transition_resolution=WorkItemTransitionResolution,
            resolve_transition=resolve_work_item_transition,
            resolve_transition_if_lifecycle_current=resolve_work_item_transition_if_lifecycle_current,
        )
    )
    configure_linear_work_item_completion_runtime(
        LinearWorkItemCompletionRuntime(
            get_execution_for_issue=get_work_item_execution_for_issue,
            get_transition_by_request=get_work_item_transition_by_request,
            get_latest_unresolved_transition=get_latest_unresolved_work_item_transition,
            transition_request=WorkItemTransitionRequest,
            begin_transition=begin_work_item_transition,
            begin_transition_if_lifecycle_current=begin_work_item_transition_if_lifecycle_current,
        )
    )
    configure_linear_legacy_work_item_runtime(
        LinearLegacyWorkItemRuntime(
            get_all_tasks=get_all_tasks,
            get_transition_by_request=get_work_item_transition_by_request,
            create_claim=create_work_item_claim,
            claim_request=WorkItemClaimRequest,
            get_active_execution=get_active_work_item_execution,
            get_execution=get_work_item_execution,
            resolve_transition=resolve_work_item_transition,
        )
    )
    configure_linear_planning_task_runtime(LinearPlanningTaskRuntime(get_all_tasks=get_all_tasks))
    configure_linear_self_echo_runtime(
        LinearSelfEchoRuntime(
            begin=begin_webhook_effect,
            mark_executing=mark_webhook_effect_executing,
            confirm=confirm_webhook_effect,
            fail=fail_webhook_effect,
            mark_outcome_unknown=mark_webhook_effect_outcome_unknown,
        )
    )


def make_container_runtime_operations() -> ContainerRuntimeOperations:
    """Return inert container operations for queue tests without a runtime."""

    def ignore_message(_folder: str, _text: str) -> None: ...

    def ignore_close(_folder: str) -> None: ...

    def ignore_gate(_folder: str, _invocation_ts: float) -> None: ...

    async def ignore_session(_folder: str) -> None: ...

    async def ignore_sessions() -> None: ...

    async def ignore_process(_process: object, _name: str) -> None: ...

    return ContainerRuntimeOperations(
        write_message=ignore_message,
        write_close_sentinel=ignore_close,
        clean_input_dir=ignore_close,
        destroy_gate=ignore_gate,
        destroy_session=ignore_session,
        destroy_all_sessions=ignore_sessions,
        graceful_stop=ignore_process,
    )


def make_container_agent_operations() -> ContainerAgentOperations:
    """Return inert container-agent operations for orchestration tests."""

    def no_session(_group_folder: object) -> None:
        return None

    return ContainerAgentOperations(
        get_session=no_session,
        fresh_container_name=AsyncMock(
            side_effect=AssertionError("test must provide a container name")
        ),
        spawn=AsyncMock(side_effect=AssertionError("test must provide a container spawn")),
        create_session=AsyncMock(
            side_effect=AssertionError("test must provide a container session")
        ),
        destroy_session=AsyncMock(),
        ensure_workspace_mcp=AsyncMock(return_value=()),
        wait_for_query=AsyncMock(return_value=True),
    )


def make_host_runtime_operations() -> HostRuntimeOperations:
    """Return inert direct-host runtime operations for orchestration tests."""

    def empty_environment(**_kwargs: object) -> dict[str, str]:
        return {}

    return HostRuntimeOperations(
        build_agent_environment=empty_environment,
        prepare_mcp=AsyncMock(),
        sessions_root=Path("sessions"),
        project_root=Path(),
        gateway_port=4000,
        prepare_host_codex_home=lambda folder, _plugins: Path("sessions") / folder / ".codex",
        host_learning_vault=lambda _folder: None,
    )


@pytest.fixture(autouse=True)
def _clean_host_mutation_cop():
    """Give non-security tests a hermetic, successful Cop boundary."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=False),
        ),
    ):
        yield


__all__ = [
    "NullChannel",
    "NullIpcDeps",
    "init_test_database",
    "make_command_matcher",
    "make_host_action_catalog",
]

_CGROUP_MEMORY_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
_UNBOUNDED_CGROUP_LIMIT = 1 << 60
_PYTEST_CONTROLLER_RESERVE_BYTES = 768 * 1024 * 1024
_PYTEST_WORKER_BUDGET_BYTES = 1024 * 1024 * 1024


def cgroup_memory_limit_bytes(
    paths: tuple[Path, ...] = _CGROUP_MEMORY_LIMIT_PATHS,
) -> int | None:
    """Read a finite cgroup v2 or v1 memory limit."""
    for path in paths:
        try:
            raw_limit = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw_limit == "max":
            continue
        try:
            limit = int(raw_limit)
        except ValueError:
            continue
        if 0 < limit < _UNBOUNDED_CGROUP_LIMIT:
            return limit
    return None


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """Scale ``-n auto`` down when the test process is memory-constrained."""
    _ = config
    configured = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
    if configured is not None and configured.isdigit() and int(configured) > 0:
        return int(configured)

    memory_limit = cgroup_memory_limit_bytes()
    if memory_limit is None:
        return None
    available_for_workers = max(0, memory_limit - _PYTEST_CONTROLLER_RESERVE_BYTES)
    memory_workers = max(1, available_for_workers // _PYTEST_WORKER_BUDGET_BYTES)
    return min(os.process_cpu_count() or 1, memory_workers)


def pytest_addoption(parser):
    """Register the opt-in dynamic action-coverage collection gate."""
    parser.addoption(
        "--action-coverage",
        action="store_true",
        default=False,
        help="require every registered action to have a pytest.mark.action test",
    )


def pytest_collection_finish(session):
    """Validate action markers after every test has been collected."""
    if not session.config.getoption("--action-coverage"):
        return
    marked_ids = [
        action_id
        for item in session.items
        for marker in item.iter_markers(name="action")
        for action_id in marker.args
    ]
    report = assess_hermetic_coverage(ACTION_SPECS, marked_ids)
    if not report.is_complete:
        raise pytest.UsageError(f"Action coverage incomplete: {report.describe()}")


# ---------------------------------------------------------------------------
# Shared helpers (plain functions, not fixtures — importable by test files)
# ---------------------------------------------------------------------------

# Cached property names that must be set via __dict__ (not model_construct).
_CACHED_PROPERTY_NAMES = frozenset(
    {
        "project_root",
        "home_dir",
        "groups_dir",
        "data_dir",
        "mount_allowlist_path",
        "worktrees_dir",
        "container_timeout",
        "idle_timeout",
        "trigger_pattern",
        "timezone",
    }
)


def make_settings(**overrides):
    """Create a Settings object with sensible defaults for testing.

    Accepts both model fields (agent, container, etc.) and cached property
    overrides (project_root, data_dir, groups_dir, etc.).

    Usage::

        s = make_settings(data_dir=tmp_path)
        s = make_settings(container=ContainerConfig(max_concurrent=3))
        s = make_settings(project_root=tmp_path, groups_dir=tmp_path / "groups")
    """
    # Separate cached properties from model fields
    cached = {k: overrides.pop(k) for k in list(overrides) if k in _CACHED_PROPERTY_NAMES}

    defaults = {
        "agent": AgentConfig(),
        "container": ContainerConfig(),
        "server": ServerConfig(),
        "logging": LoggingConfig(),
        "secrets": SecretsConfig(),
        "profiles": {},
        "workspaces": {},
        "commands": CommandWordsConfig(),
        "scheduler": SchedulerConfig(),
        "canary": CanaryConfig(),
        "intervals": IntervalsConfig(),
        "queue": QueueConfig(),
        "security": SecurityConfig(),
        "command_center": CommandCenterConfig(),
        "plugins": {},
        "jobs": {},
    }
    defaults.update(overrides)
    s = Settings.model_construct(**defaults)

    for key, value in cached.items():
        s.__dict__[key] = value

    return s


def make_command_matcher(settings: Settings) -> CommandMatcher:
    """Build the runtime command value that production composes from settings."""
    return CommandMatcher.from_values(settings.trigger_pattern, settings.commands.model_dump())


def make_host_action_catalog(
    *tool_names: str,
    handler,
    read_tools: tuple[str, ...] = (),
    approval_mode: ApprovalMode = ApprovalMode.EXACT_REQUEST,
) -> HostActionCatalog:
    """Build a typed catalog for dispatch-focused tests.

    Catalog validation is covered separately. These tests intentionally use
    synthetic tool names so they can isolate dispatch and approval behavior.
    """
    actions = []
    for tool_name in tool_names:
        access = HostActionAccess.READ if tool_name in read_tools else HostActionAccess.WRITE
        actions.append(
            HostActionDescriptor(
                capability=CapabilityDescriptor(
                    id=CapabilityId(f"test.{tool_name.replace('_', '.')}"),
                    kind=CapabilityKind.HOST_ACTION,
                    owner="tests",
                    summary=f"Exercise the {tool_name} test action.",
                    action_ids=(ActionId(f"test.{tool_name.replace('_', '.')}"),),
                ),
                tool_name=HostToolName(tool_name),
                handler=handler,
                access=access,
                approval=ApprovalContract(mode=approval_mode),
                idempotency=IdempotencyContract(
                    IdempotencyMode.NOT_REQUIRED
                    if access is HostActionAccess.READ
                    else IdempotencyMode.IPC_REQUEST_ID
                ),
                audit=AuditContract(),
            )
        )
    return HostActionCatalog(actions=tuple(actions))


class NullIpcDeps:
    """No-op stand-in for every method on ``IpcDeps``.

    ``beartype_this_package()`` validates fake/mock arguments against the real
    ``IpcDeps`` Protocol at call time — structurally, by attribute name, not
    by behavior. Subclass this and override only the methods your test
    actually exercises; the rest are satisfied for free instead of each fake
    class hand-rolling all fifteen methods.
    """

    async def broadcast_to_channels(self, jid, event) -> None: ...

    async def broadcast_host_message(self, jid, text) -> None: ...

    async def broadcast_system_notice(self, jid, text) -> None: ...

    def workspaces(self) -> dict:
        return {}

    def register_workspace(self, profile) -> None: ...

    async def sync_group_metadata(self, *, force) -> None: ...

    async def get_available_groups(self) -> list:
        return []

    def write_groups_snapshot(
        self,
        group_folder,
        available_groups,
        registered_jids,
        *,
        is_admin,
    ) -> None: ...

    def has_active_session(self, group_folder) -> bool:
        return False

    async def clear_session(self, group_folder) -> None: ...

    def get_active_sessions(self) -> dict:
        return {}

    async def clear_chat_history(self, chat_jid) -> None: ...

    def enqueue_message_check(self, group_jid) -> None: ...

    def channels(self) -> list:
        return []

    def pending_question_store(self):
        return _NullPendingQuestionStore()

    def scheduled_work_store(self):
        return _TestScheduledWorkStore()

    async def request_deploy(
        self,
        *,
        chat_jid=None,
        commit_sha="",
        rebuild=False,
        resume_prompt="",
    ) -> None: ...

    async def trigger_deploy(self, previous_sha, *, rebuild=True) -> None: ...

    async def create_periodic_agent(self, request) -> None: ...

    async def get_scheduled_work_status(self, *, source_group, is_admin) -> tuple[list, list]:
        return [], []

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

    async def sweep_expired_questions(self, _write_expiration_response) -> list[dict]:
        return []

    def skill_access_status(self, _group_folder, _skill_name) -> str:
        return "unavailable"

    async def load_cop_inspection_context(self, _chat_jid):
        return CopInspectionContext(availability=CopContextAvailability.AVAILABLE)


class _NullPendingQuestionStore:
    def create(self, **kwargs) -> None:
        del kwargs

    def update_message_id(self, request_id, source_group, message_id) -> None:
        del request_id, source_group, message_id

    def resolve(self, request_id, source_group) -> None:
        del request_id, source_group


class _TestScheduledWorkStore:
    create_task = staticmethod(create_task)
    create_host_job = staticmethod(create_host_job)
    get_task_by_id = staticmethod(get_task_by_id)
    get_host_job_by_id = staticmethod(get_host_job_by_id)
    update_task = staticmethod(update_task)
    update_host_job = staticmethod(update_host_job)
    resume_task = staticmethod(resume_task)
    delete_task = staticmethod(delete_task)
    delete_host_job = staticmethod(delete_host_job)


class NullChannel:
    """No-op stand-in for every method on ``Channel``.

    Same rationale as ``NullIpcDeps``: satisfies the ``Channel`` Protocol's
    isinstance check structurally so fakes only need to override the
    handful of members a given test actually exercises.
    """

    name = "null-channel"
    formatter = None

    async def connect(self) -> None: ...

    async def send_event(self, jid, event) -> None: ...

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid) -> bool:
        return False

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid, since) -> InboundFetchResult:
        return InboundFetchResult(messages=[])


# ---------------------------------------------------------------------------
# Secret scrubbing — detect credentials in both SecretStr and plain str fields
# ---------------------------------------------------------------------------

# Known credential prefixes / patterns in plain strings.
# Catches tokens even if someone puts them in a non-SecretStr field or URL.
_CREDENTIAL_RE = re.compile(
    r"xoxb-"  # Slack bot token
    r"|xapp-"  # Slack app-level token
    r"|sk-ant-"  # Anthropic API key
    r"|sk-proj-"  # OpenAI API key
    r"|ghp_|gho_|ghs_"  # GitHub PAT / OAuth / server token
    r"|://[^/\s]*:[^@\s]+@",  # credentials embedded in URLs  (user:pass@host)
)


def _scrub_model(obj: BaseModel) -> None:
    """Recursively nullify SecretStr fields and credential-bearing strings.

    Walks all Pydantic model fields (including nested sub-models and dicts of
    sub-models) and replaces:
    - ``SecretStr`` values → ``None``
    - Plain ``str`` values matching ``_CREDENTIAL_RE`` → ``""``
    """
    for name in type(obj).model_fields:
        val = getattr(obj, name, None)
        if val is None:
            continue

        if isinstance(val, SecretStr):
            setattr(obj, name, None)
        elif isinstance(val, BaseModel):
            _scrub_model(val)
        elif isinstance(val, str) and _CREDENTIAL_RE.search(val):
            setattr(obj, name, "")
        elif isinstance(val, dict):
            for v in val.values():
                if isinstance(v, BaseModel):
                    _scrub_model(v)


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _clean_git_env():
    """Strip git env vars that hook runners can leak during their stash cycle.

    Hook runners can set GIT_INDEX_FILE (and potentially GIT_DIR, GIT_WORK_TREE)
    before invoking checks. Tests that create temporary git repos inherit these
    variables, causing ``git worktree add`` and similar commands to fail with
    ``fatal: .git/index: index file open failed: Not a directory``.
    """
    for var in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        os.environ.pop(var, None)


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    """Ensure each test starts with a clean Settings singleton.

    Uses ``make_settings()`` to build from pure model defaults — no files,
    no .env, no file I/O. Tests are fully isolated from production config.
    Direct ``Settings()`` calls still read real environment variables, but not
    repo-local config files or dotenv files.

    Tests that mock ``get_settings()`` at the call site are unaffected — their
    mock takes precedence over the cached singleton.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    with repository_settings_sources(enabled=False):
        safe = make_settings()
        monkeypatch.setattr("pynchy.config.settings._state.settings", safe)
        configure_ipc_base_dir(safe.data_dir / "ipc")
        configure_approval_state_root(safe.data_dir / "approvals")
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
                code_revision=lambda: "test-revision",
            )
        )
        configure_pending_questions_ipc_base_dir(safe.data_dir / "ipc")
        configure_personalized_skills_root(safe.project_root)
        configure_git_default_cwd(safe.project_root)
        configure_allowed_message_filter(access.filter_allowed_messages)
        configure_workspace_placement_for(safe)
        configure_learning_paths_for(safe)
        configure_skill_activation_for(safe)
        configure_linear_accounts_for(safe)

        def mount_agent_homes(folder: str, plugins: object | None) -> AgentHomeMounts:
            homes = prepare_agent_homes(folder, plugins)
            return AgentHomeMounts(
                claude_home=homes.claude_home,
                codex_home=homes.codex_home,
                vault_mount_root=(
                    prepare_vault_mount_root(homes.learning_paths)
                    if homes.learning_paths is not None
                    else None
                ),
                vault_mount_path=(
                    homes.learning_paths.vault_mount_path
                    if homes.learning_paths is not None
                    else None
                ),
            )

        configure_mount_operations(
            MountOperations(
                prepare_agent_homes=mount_agent_homes,
                repo_container_path=lambda slug: f"/workspace/repos/{slug}",
                runtime_name=lambda: "docker",
            )
        )
        configure_container_spawn_runtime(
            container_cli="docker",
            ensure_agent_image=lambda **_kwargs: None,
            resolve_repo_mounts=lambda _folder, _repos: RepoMountResolution(),
        )
        configure_container_process_runtime(
            container_cli="docker",
            is_apple_runtime=False,
            container_is_running=lambda _name: False,
        )
        configure_gateway_runtime(is_apple_container=False)
        configure_vault_mount_mirror(enabled=False)
        yield


@pytest.fixture(autouse=True, scope="session")
def _close_test_database():
    """Close the aiosqlite connection after all tests complete.

    Uses ``stop()`` + thread join rather than ``await close()`` because
    the connection was created on a function-scoped event loop (during a
    test).  ``stop()`` bypasses the event loop by putting the close
    command directly on the worker thread's queue.

    This is a sync fixture so it runs during session teardown regardless
    of event loop state — avoids the race where pytest-xdist workers
    close the loop before an async session fixture can tear down.
    """
    yield
    close_test_database()


# ---------------------------------------------------------------------------
# Reusable fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def make_msg():
    """Factory fixture for creating test messages with defaults."""

    def _make(
        *,
        message_id: str = "1",
        chat_jid: str = "group@g.us",
        sender: str = "123@s.whatsapp.net",
        sender_name: str = "Alice",
        content: str = "hello",
        timestamp: str = "2024-01-01T00:00:00.000Z",
        is_from_me: bool | None = None,
    ) -> NewMessage:
        return NewMessage(
            id=message_id,
            chat_jid=chat_jid,
            sender=sender,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
            is_from_me=is_from_me,
        )

    return _make
