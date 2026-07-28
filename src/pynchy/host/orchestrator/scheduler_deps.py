"""Runtime dependency contract for scheduled agent execution."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves queue annotations.
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from pynchy.turn_outcomes import TurnOutcome

from pynchy.linear_plan_types import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    LinearPlanReviewRequest,
    LinearPlanReviewResult,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    ContainerOutput,
    OnOutput,
    OutboundEvent,
    RuntimeTarget,
    ScheduledTask,
    WorkspaceProfile,
)


@runtime_checkable
class StartupReadinessGate(Protocol):
    """Wait capability for work that depends on completed startup recovery."""

    async def wait(self) -> None: ...


@dataclass(frozen=True)
class ScheduledExecutionLifecycle:
    """Lifecycle facts required to classify one scheduled execution."""

    execution_id: str
    status: str
    has_explicit_outcome: bool


@runtime_checkable
class ScheduledCompletionDeps(Protocol):
    """Read the lifecycle fact needed by scheduled completion policy."""

    async def scheduled_execution_lifecycle(
        self, task_id: str
    ) -> ScheduledExecutionLifecycle | None: ...


_QueueResultT = TypeVar("_QueueResultT")


@runtime_checkable
class ScheduledQueue(Protocol):
    """Queue operations used by scheduled and interactive Temporal activities."""

    async def run_message_turn(self, target: RuntimeTarget) -> TurnOutcome: ...

    async def run_serialized_task(
        self,
        target: RuntimeTarget,
        task_id: str,
        run: Callable[[], Awaitable[_QueueResultT]],
    ) -> _QueueResultT: ...


@runtime_checkable
class SchedulerDependencies(ScheduledCompletionDeps, Protocol):
    """Dependencies shared by the task scheduler and Temporal activities."""

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    @property
    def queue(self) -> ScheduledQueue: ...

    @property
    def startup_readiness(self) -> StartupReadinessGate: ...

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

    def sync_personalization(self, project_root: Path) -> str: ...

    async def review_linear_plan(
        self,
        request: LinearPlanReviewRequest,
    ) -> LinearPlanReviewResult: ...

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
