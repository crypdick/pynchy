"""Runtime dependency contracts for scheduled agent turns."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pynchy.agent_protocol.api import (
    ContainerOutput,
    OnOutput,
)
from pynchy.identifiers import RuntimeId
from pynchy.workspace.api import (
    WorkspaceProfile,
)


@runtime_checkable
class ScheduledTurnQueue(Protocol):
    def close_stdin(self, runtime_id: RuntimeId) -> None: ...

    def boundary_interrupt_requested(self, runtime_id: RuntimeId) -> bool: ...

    async def interrupt_after_tool_result(self, runtime_id: RuntimeId) -> bool: ...


@runtime_checkable
class ScheduledTurnDeps(Protocol):
    @property
    def queue(self) -> ScheduledTurnQueue: ...

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
