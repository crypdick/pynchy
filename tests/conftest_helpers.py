"""Reusable test configuration helpers and protocol doubles."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from pynchy.actions.api import ActionId
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
)
from pynchy.host.container_manager.security.cop import (
    CopContextAvailability,
    CopInspectionContext,
)
from pynchy.host.orchestrator.agent_runner import ContainerAgentOperations
from pynchy.host.orchestrator.api import (
    ContainerRuntimeOperations,
    cancel_scheduled_host_job,
    cancel_scheduled_task,
    execute_action_intent,
    policy_approval_timestamp,
    prepare_action_intent,
)
from pynchy.host.orchestrator.host_execution import HostExecutionCwd, HostRuntimeOperations
from pynchy.host.orchestrator.messaging.deps import CommandMatcher
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
)
from pynchy.state import (
    approve_action_intent,
    create_host_job,
    create_task,
    deny_action_intent,
    expire_action_intent,
    fail_action_intent,
    get_action_intent_by_request,
    get_conversation_control_binding,
    get_conversation_control_by_thread,
    get_host_job_by_id,
    get_task_by_id,
    mark_action_intent_awaiting_approval,
    resume_task,
    update_host_job,
    update_task,
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
        start_session=AsyncMock(
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
        resolve_routed_host_cwd=lambda _folder, cwd, _repo_accesses, *, recovered: HostExecutionCwd(
            cwd
        ),
    )


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
    cached = {key: overrides.pop(key) for key in list(overrides) if key in _CACHED_PROPERTY_NAMES}

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
    settings = Settings.model_construct(**defaults)

    for key, value in cached.items():
        settings.__dict__[key] = value

    return settings


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

    async def wake_worktree_conflict(self, jid) -> None: ...

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

    def persist_capability_approval(self, _group_folder, _capability_id) -> None: ...

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
    cancel_task = staticmethod(cancel_scheduled_task)
    cancel_host_job = staticmethod(cancel_scheduled_host_job)


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
