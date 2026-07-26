"""Runtime dependency contracts for scheduled agent turns."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pynchy.host.container_manager import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    OnOutput,
)
from pynchy.host.orchestrator.threads import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    EnsuredThread,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    ContainerOutput,
    WorkspaceProfile,
)


@runtime_checkable
class ScheduledTurnQueue(Protocol):
    def close_stdin(self, chat_jid: str) -> None: ...


@runtime_checkable
class ScheduledTurnDeps(Protocol):
    @property
    def queue(self) -> ScheduledTurnQueue: ...

    async def supports_thread_creation(self, parent_jid: str) -> bool: ...

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str: ...

    async def find_thread(self, parent_jid: str, name: str) -> str | None: ...

    async def add_thread_participants(
        self,
        child_jid: str,
        participant_ids: tuple[str, ...],
    ) -> None: ...

    async def ensure_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> EnsuredThread: ...

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
