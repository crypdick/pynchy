"""Runtime dependency contract for scheduled agent execution."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves queue annotations.
    Awaitable,
    Callable,
)
from contextlib import (
    AbstractContextManager,  # noqa: TC003 - beartype resolves protocol annotations.
)
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - beartype resolves runtime config annotations.
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from pynchy.turn_outcomes import TurnOutcome

from pynchy.agent_protocol.api import (  # noqa: TC001 - beartype resolves annotations.
    ContainerOutput,
    OnOutput,
)
from pynchy.canary_contracts import (  # noqa: TC001 - beartype resolves annotations.
    CanaryRun,
)
from pynchy.learning_packets import (  # noqa: TC001 - beartype resolves annotations.
    LearningPacket,
)
from pynchy.linear_plan_types import (  # noqa: TC001 - beartype resolves annotations.
    LinearPlanReviewAdmission,
    LinearPlanReviewRequest,
    LinearPlanReviewResult,
)
from pynchy.plugins.api import OutboundEvent  # noqa: TC001 - beartype resolves annotations.
from pynchy.scheduling.api import (
    ScheduledTask,  # noqa: TC001 - beartype resolves annotations.
)
from pynchy.workspace.api import (  # noqa: TC001 - beartype resolves annotations.
    RuntimeTarget,
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


@dataclass(frozen=True)
class ConfigHostCronJob:
    """Validated host-cron data needed after configuration is loaded."""

    command: str
    schedule: str
    cwd: str | None
    timeout_seconds: int | None
    quiet_on_success: bool
    memory_enabled: bool


@dataclass(frozen=True)
class SchedulerRuntimeConfig:
    """Resolved scheduler settings owned by the orchestrator composition root."""

    temporal_address: str
    temporal_namespace: str
    temporal_task_queue: str
    reconcile_schedules: bool
    poll_interval: float
    timezone: str | None
    git_sync_interval_seconds: int
    channel_reconciliation_interval_seconds: int
    auto_deploy: bool
    idle_timeout: float
    groups_dir: Path
    project_root: Path
    admin_workspace: str | None
    queue_max_retries: int
    queue_base_retry_seconds: float
    learning_max_attempts: int
    canary_enabled: bool
    canary_schedule: str
    canary_target_profile: str
    canary_scenario_ids: tuple[str, ...]
    external_repo_sync_slugs: tuple[str, ...]
    config_host_cron_jobs: dict[str, ConfigHostCronJob]


@dataclass
class HostSyncState:
    """Mutable host-repository baseline shared with the Git adapter."""

    last_origin_sha: str | None
    deployed_sha: str
    config_hash: str
    local_head: str | None = None
    offered_sha: str = ""


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

    scheduler_runtime: SchedulerRuntimeConfig

    def automation_memory_dir(self, task_id: str) -> AbstractContextManager[Path | None]: ...

    async def broadcast_to_channels(self, jid: str, event: OutboundEvent) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None: ...

    async def run_declared_canaries(
        self, target_profile: str, scenario_ids: tuple[str, ...]
    ) -> list[CanaryRun]: ...

    async def run_learning_review(self, packet: LearningPacket) -> str: ...

    async def reconcile_linear_work_items(self) -> int | None: ...

    async def process_linear_plan_review_admission(
        self,
        admission: LinearPlanReviewAdmission,
        *,
        attempt: int = 1,
        reset_context: Callable[[str], Awaitable[None]] | None = None,
    ) -> bool: ...

    async def reset_linear_plan_review_context(self, chat_jid: str) -> None: ...

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

    async def run_agent(  # noqa: PLR0913 - mirrors the orchestrator contract.
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
        automation_memory_dir: Path | None = None,
    ) -> str: ...

    async def handle_streamed_output(
        self,
        chat_jid: str,
        group: WorkspaceProfile,
        result: ContainerOutput,
        *,
        turn_id: str | None = None,
    ) -> bool: ...
