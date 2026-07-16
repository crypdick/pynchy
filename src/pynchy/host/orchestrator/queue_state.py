"""State records owned by :mod:`pynchy.host.orchestrator.concurrency`."""

from __future__ import annotations

import asyncio  # noqa: TC003, RUF100 - beartype validates dataclass annotations at runtime.
from collections import deque
from collections.abc import (  # noqa: TC003, RUF100 - beartype validates dataclass annotations at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass, field

import pynchy.host.container_manager.security.gate as security_gate
from pynchy.logger import logger


@dataclass
class QueuedTask:
    """One scheduled task awaiting execution for a group."""

    id: str
    group_jid: str
    fn: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ExternalProcessLease:
    """Identifies one host process and whether it owns the group's active slot."""

    group_jid: str
    generation: int
    owns_group_slot: bool


@dataclass
class GroupState:
    """Transient queue state for one group."""

    active: bool = False
    active_is_task: bool = False
    pending_messages: bool = False
    pending_tasks: deque[QueuedTask] = field(default_factory=deque)
    process: asyncio.subprocess.Process | None = None
    container_name: str | None = None
    group_folder: str | None = None
    invocation_ts: float = 0.0
    retry_count: int = 0
    defer_interrupt_until_tool_result: bool = False
    is_host_process: bool = False
    is_external_run: bool = False
    external_process_lease: ExternalProcessLease | None = None
    boundary_interrupt_requested: bool = False

    def register_process(
        self,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        group_folder: str | None,
        invocation_ts: float,
        *,
        is_host_process: bool,
    ) -> None:
        """Associate the active process with this group state."""
        self.process = proc
        self.container_name = container_name
        if group_folder:
            self.group_folder = group_folder
        self.invocation_ts = invocation_ts
        self.is_host_process = is_host_process

    def acquire_external_process(self, group_jid: str, generation: int) -> ExternalProcessLease:
        """Reserve this state for a direct host process before it is spawned."""
        if self.external_process_lease is not None:
            raise RuntimeError(f"A host process is already registered for {group_jid}")
        lease = ExternalProcessLease(
            group_jid=group_jid,
            generation=generation,
            owns_group_slot=not self.active,
        )
        self.external_process_lease = lease
        if lease.owns_group_slot:
            self.active = True
            self.is_external_run = True
        return lease

    def register_external_process(
        self,
        lease: ExternalProcessLease,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        group_folder: str | None,
        invocation_ts: float,
    ) -> bool:
        """Attach a spawned host process only when it still owns this state."""
        if self.external_process_lease != lease:
            logger.warning(
                "Ignoring stale host process registration",
                group_jid=lease.group_jid,
                generation=lease.generation,
            )
            return False
        self.register_process(
            proc,
            container_name,
            group_folder,
            invocation_ts,
            is_host_process=True,
        )
        return True

    def release_external_process(self, lease: ExternalProcessLease) -> bool:
        """Release this state only when it still belongs to *lease*."""
        if self.external_process_lease != lease:
            logger.warning(
                "Ignoring stale host process release",
                group_jid=lease.group_jid,
                generation=lease.generation,
            )
            return False
        has_pending_messages = self.pending_messages
        self.external_process_lease = None
        if not lease.owns_group_slot:
            return has_pending_messages
        self.release()
        self.pending_messages = False
        return has_pending_messages

    def release(self) -> None:
        """Reset transient per-run state when a container slot is freed."""
        if self.group_folder and self.invocation_ts:
            security_gate.destroy_gate(self.group_folder, self.invocation_ts)
        self.active = False
        self.active_is_task = False
        self.process = None
        self.container_name = None
        self.group_folder = None
        self.invocation_ts = 0.0
        self.defer_interrupt_until_tool_result = False
        self.is_host_process = False
        self.is_external_run = False
        self.external_process_lease = None
        self.boundary_interrupt_requested = False
