"""State records owned by :mod:`pynchy.host.orchestrator.concurrency`."""

from __future__ import annotations

import asyncio  # noqa: TC003 - beartype validates dataclass annotations at runtime.
from collections import deque
from collections.abc import (  # noqa: TC003 - beartype validates dataclass annotations at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass, field

from pynchy.identifiers import (
    RuntimeId,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.logger import logger
from pynchy.workspace.api import (
    RuntimeTarget,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)


@dataclass(eq=False)
class QueuedTask:
    """One owned operation awaiting execution for a runtime."""

    id: str
    fn: Callable[[], Awaitable[None]]
    cancel: Callable[[], None]
    is_interactive: bool = False


@dataclass(frozen=True)
class HostProcessLease:
    """Identifies one direct host process that owns a runtime's active slot."""

    runtime_id: RuntimeId
    generation: int
    owns_slot: bool


@dataclass
class GroupState:
    """Transient queue state for one stable runtime."""

    target: RuntimeTarget
    active: bool = False
    pending_tasks: deque[QueuedTask] = field(default_factory=deque)
    active_task: QueuedTask | None = None
    process: asyncio.subprocess.Process | None = None
    container_name: str | None = None
    invocation_ts: float = 0.0
    defer_interrupt_until_tool_result: bool = False
    is_host_process: bool = False
    is_external_run: bool = False  # noqa: V107
    host_process_lease: HostProcessLease | None = None
    boundary_interrupt_requested: bool = False

    @property
    def active_is_task(self) -> bool:
        return self.active_task is not None and not self.active_task.is_interactive

    @property
    def pending_messages(self) -> bool:
        return any(task.is_interactive for task in self.pending_tasks)

    def register_process(
        self,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        invocation_ts: float,
        *,
        is_host_process: bool,
    ) -> bool:
        """Associate a process unless terminal control already stopped this turn."""
        if self.boundary_interrupt_requested:
            logger.info(
                "Ignoring process registration after boundary interrupt",
                runtime_id=self.target.id,
                container_name=container_name,
            )
            return False
        self.process = proc
        self.container_name = container_name
        self.invocation_ts = invocation_ts
        self.is_host_process = is_host_process
        return True

    def acquire_host_process(self, generation: int) -> HostProcessLease:
        """Attach a host process to a queued turn or reserve an idle runtime."""
        if self.host_process_lease is not None:
            raise RuntimeError(f"A host process is already registered for {self.target.id}")
        owns_slot = not self.active
        lease = HostProcessLease(
            runtime_id=self.target.id,
            generation=generation,
            owns_slot=owns_slot,
        )
        self.host_process_lease = lease
        if owns_slot:
            self.active = True
            self.is_external_run = True  # noqa: V101
        return lease

    def register_host_process(
        self,
        lease: HostProcessLease,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        invocation_ts: float,
    ) -> bool:
        """Attach a spawned host process only when it still owns this state."""
        if self.host_process_lease != lease:
            logger.warning(
                "Ignoring stale host process registration",
                runtime_id=lease.runtime_id,
                generation=lease.generation,
            )
            return False
        return self.register_process(
            proc,
            container_name,
            invocation_ts,
            is_host_process=True,
        )

    def release_host_process(self, lease: HostProcessLease) -> bool:
        """Release this state only when it still belongs to *lease*."""
        if self.host_process_lease != lease:
            logger.warning(
                "Ignoring stale host process release",
                runtime_id=lease.runtime_id,
                generation=lease.generation,
            )
            return False
        has_pending_messages = self.pending_messages
        if not lease.owns_slot:
            self.host_process_lease = None
            self.process = None
            self.container_name = None
            self.invocation_ts = 0.0
            self.is_host_process = False
            return has_pending_messages
        self.host_process_lease = None
        self.release()
        return has_pending_messages

    def release(self) -> None:
        """Reset transient per-run state when a container slot is freed."""
        self.active = False
        self.active_task = None
        self.process = None
        self.container_name = None
        self.invocation_ts = 0.0
        self.defer_interrupt_until_tool_result = False
        self.is_host_process = False
        self.is_external_run = False  # noqa: V101
        self.host_process_lease = None
        self.boundary_interrupt_requested = False
