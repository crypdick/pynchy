"""Dependency protocol and helpers for IPC processing."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pynchy.host.container_manager.ipc.protocol import (  # noqa: TC001, RUF100 - beartype resolves IPC dependency protocol signatures at runtime.
    CreatePeriodicAgentRequest,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves IPC dependency protocol signatures at runtime.
    Channel,
    HostJob,
    OutboundEvent,
    ScheduledTask,
    WorkspaceProfile,
)


@runtime_checkable
class IpcDeps(Protocol):
    """Dependencies for IPC processing."""

    async def broadcast_to_channels(self, jid: str, event: OutboundEvent) -> None: ...

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, jid: str, text: str) -> None: ...

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


@runtime_checkable
class PendingQuestionStore(Protocol):
    """Persistence boundary for interactive questions raised over IPC."""

    def create(  # noqa: PLR0913, RUF100 - file-backed payload mirrors the channel callback contract.
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

    async def delete_task(self, task_id: str) -> None: ...

    async def delete_host_job(self, job_id: str) -> None: ...


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
