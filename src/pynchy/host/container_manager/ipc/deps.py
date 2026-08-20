"""Dependency protocol and helpers for IPC processing."""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves IPC callback annotations at runtime.
)
from typing import Any, Protocol, runtime_checkable

from pynchy.action_intents import (  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.
    ActionIntent,
)
from pynchy.conversation.api import (  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.
    ConversationControlBinding,
    ConversationId,
)
from pynchy.host.container_manager.ipc.protocol import (  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.
    CreatePeriodicAgentRequest,
)
from pynchy.host.container_manager.security.cop import (  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.
    CopInspectionContext,
)
from pynchy.identifiers import (
    ChatJid,  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.
)
from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.
    Channel,
    HostActionDescriptor,
    OutboundEvent,
)
from pynchy.scheduling.api import (  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.
    HostJob,
    ScheduledTask,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves IPC dependency protocol signatures at runtime.
)


@runtime_checkable
class IpcDeps(Protocol):
    """Dependencies for IPC processing."""

    async def broadcast_to_channels(self, jid: str, event: OutboundEvent) -> None: ...

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, jid: str, text: str) -> None: ...

    async def wake_worktree_conflict(self, jid: str) -> None: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    def register_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def sync_group_metadata(self, *, force: bool) -> None: ...

    async def get_available_groups(self) -> list[Any]: ...

    def write_groups_snapshot(
        self,
        group_folder: str,
        available_groups: list[Any],
        registered_jids: set[str],
        *,
        is_admin: bool,
    ) -> None: ...

    def has_active_session(self, group_folder: str) -> bool: ...

    async def clear_session(self, group_folder: str) -> None: ...

    def get_active_sessions(self) -> dict[str, str]: ...

    async def clear_chat_history(self, chat_jid: str) -> None: ...

    def enqueue_message_check(self, group_jid: str) -> None: ...

    def channels(self) -> list[Channel]: ...

    async def request_deploy(
        self,
        *,
        chat_jid: str | None,
        commit_sha: str,
        rebuild: bool,
        resume_prompt: str,
    ) -> None: ...

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None: ...

    async def create_periodic_agent(self, request: CreatePeriodicAgentRequest) -> None: ...

    async def get_scheduled_work_status(
        self,
        *,
        source_group: str,
        is_admin: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...

    async def prepare_action_intent(
        self,
        action: HostActionDescriptor,
        data: dict[str, Any],
        *,
        workspace: str,
        chat_jid: str,
        request_id: str,
    ) -> tuple[ActionIntent | None, dict[str, Any] | None]: ...

    async def execute_action_intent(
        self,
        action: HostActionDescriptor,
        data: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]: ...

    def policy_approval_timestamp(self) -> str: ...

    async def approve_action_intent(
        self,
        request_id: str,
        *,
        approver: str,
        approved_at: str,
        policy_decision: str,
    ) -> ActionIntent: ...

    async def deny_action_intent(self, request_id: str, *, reason: str) -> ActionIntent | None: ...

    async def fail_action_intent(self, request_id: str, *, reason: str) -> ActionIntent | None: ...

    async def expire_action_intent(
        self, request_id: str, *, reason: str
    ) -> ActionIntent | None: ...

    async def mark_action_intent_awaiting_approval(
        self, request_id: str, *, policy_decision: str
    ) -> ActionIntent: ...

    async def get_conversation_control_by_thread(
        self, thread_jid: ChatJid
    ) -> ConversationControlBinding | None: ...

    async def load_cop_inspection_context(self, chat_jid: str) -> CopInspectionContext: ...

    async def get_action_intent_by_request(self, request_id: str) -> ActionIntent | None: ...

    async def get_conversation_control_binding(
        self, conversation_id: ConversationId
    ) -> ConversationControlBinding | None: ...

    async def sweep_expired_questions(
        self, write_expiration_response: Callable[[str, str, str], None]
    ) -> list[dict[str, Any]]: ...

    def skill_access_status(self, group_folder: str, skill_name: str) -> str: ...

    def persist_capability_approval(self, group_folder: str, capability_id: str) -> None: ...


@runtime_checkable
class PendingQuestionStore(Protocol):
    """Persistence boundary for interactive questions raised over IPC."""

    def create(  # noqa: PLR0913 - file-backed payload mirrors the channel callback contract.
        self,
        *,
        request_id: str,
        source_group: str,
        chat_jid: str,
        channel_name: str,
        session_id: str,
        questions: list[dict[str, Any]],
    ) -> None: ...

    def update_message_id(self, request_id: str, source_group: str, message_id: str) -> None: ...

    def resolve(self, request_id: str, source_group: str) -> None: ...


@runtime_checkable
class AskUserDeps(IpcDeps, Protocol):
    """Narrow IPC dependency capability for interactive questions."""

    def pending_question_store(self) -> PendingQuestionStore: ...


@runtime_checkable
class ScheduledWorkStore(Protocol):
    """Persistence capability for IPC-owned scheduled work."""

    async def create_task(self, task: ScheduledTask) -> None: ...

    async def create_host_job(self, job: dict[str, Any]) -> None: ...

    async def get_task_by_id(self, task_id: str) -> ScheduledTask | None: ...

    async def get_host_job_by_id(self, job_id: str) -> HostJob | None: ...

    async def update_task(self, task_id: str, updates: dict[str, Any]) -> None: ...

    async def update_host_job(self, job_id: str, updates: dict[str, Any]) -> None: ...

    async def resume_task(self, task_id: str) -> None: ...

    async def cancel_task(self, task_id: str) -> None: ...

    async def cancel_host_job(self, job_id: str) -> None: ...


@runtime_checkable
class TaskHandlerDeps(IpcDeps, Protocol):
    """Narrow IPC dependency capability for scheduled-work handlers."""

    def scheduled_work_store(self) -> ScheduledWorkStore: ...


@runtime_checkable
class MessagingSourceHealth(Protocol):
    """Read-only host projection for messaging-source health."""

    def configured_connections(self) -> dict[str, str]: ...

    def personal_providers(self) -> tuple[str, ...]: ...

    def personal_provider_for(self, source_name: str) -> str | None: ...

    async def project_personal_source(self, provider: str) -> dict[str, object]: ...

    async def get_latest_inbound_timestamp(self, chat_jids: tuple[str, ...]) -> str | None: ...


@runtime_checkable
class SourceHealthDeps(IpcDeps, Protocol):
    """Narrow IPC dependency capability for source-health queries."""

    def messaging_source_health(self) -> MessagingSourceHealth: ...


@runtime_checkable
class IpcMessageDeps(IpcDeps, Protocol):
    """Narrow IPC dependency capability for inbound-message presentation."""

    def default_agent_name(self) -> str: ...


def resolve_chat_jid(source_group: str, deps: IpcDeps) -> str | None:
    """Look up the chat JID for a group folder from the workspace registry."""
    for jid, ws in deps.workspaces().items():
        if ws.folder == source_group:
            return jid
    return None


def resolve_workspace_by_folder(source_group: str, deps: IpcDeps) -> WorkspaceProfile | None:
    """Look up a WorkspaceProfile by its folder name."""
    return next(
        (ws for ws in deps.workspaces().values() if ws.folder == source_group),
        None,
    )
