"""Runtime dependency contract for scheduled agent execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pynchy.host.orchestrator.concurrency import GroupQueue
    from pynchy.host.orchestrator.startup_readiness import StartupReadiness

from pynchy.host.container_manager import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    OnOutput,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    ContainerOutput,
    OutboundEvent,
    ScheduledTask,
    WorkspaceProfile,
)


@runtime_checkable
class SchedulerDependencies(Protocol):
    """Dependencies shared by the task scheduler and Temporal activities."""

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def startup_readiness(self) -> StartupReadiness: ...

    async def broadcast_to_channels(self, jid: str, event: OutboundEvent) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None: ...

    async def reset_scheduled_context(
        self,
        task: ScheduledTask,
        group: WorkspaceProfile,
        occurrence_id: str,
    ) -> None: ...

    async def save_state(self) -> None: ...

    async def run_agent(  # noqa: PLR0913, RUF100 - mirrors the orchestrator contract.
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict[str, Any]],
        on_output: OnOutput | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        repo_access_override: str | None = None,
        input_source: str = "user",
        turn_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> str: ...

    async def handle_streamed_output(
        self,
        chat_jid: str,
        group: WorkspaceProfile,
        result: ContainerOutput,
        *,
        turn_id: str | None = None,
    ) -> bool: ...
