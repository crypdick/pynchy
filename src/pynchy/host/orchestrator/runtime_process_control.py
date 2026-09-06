"""Process lifecycle and IPC control for queued execution runtimes."""

from __future__ import annotations

import asyncio  # beartype resolves process annotations.
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from pynchy.host.orchestrator.host_runner import stop_host_process
from pynchy.host.orchestrator.queue_state import HostProcessLease
from pynchy.host.orchestrator.runtime_registry import (
    RuntimeRegistry,
)
from pynchy.identifiers import (
    RuntimeId,
)
from pynchy.logger import logger
from pynchy.workspace.api import (
    RuntimeTarget,
)


@dataclass(frozen=True)
class ContainerRuntimeOperations:
    """Container operations selected by the application composition root."""

    write_message: Callable[[str, str], None]
    write_close_sentinel: Callable[[str], None]
    clean_input_dir: Callable[[str], None]
    destroy_gate: Callable[[str, float], None]
    destroy_session: Callable[[str], Awaitable[None]]
    destroy_all_sessions: Callable[[], Awaitable[None]]
    graceful_stop: Callable[[asyncio.subprocess.Process, str], Awaitable[None]]


class RuntimeProcessControl:
    """Attach, observe, interrupt, and release runtime worker processes."""

    def __init__(
        self,
        registry: RuntimeRegistry,
        container_runtime: ContainerRuntimeOperations,
    ) -> None:
        self._registry = registry
        self._container_runtime = container_runtime
        self._next_host_process_generation = 0

    def register_process(
        self,
        runtime_id: RuntimeId,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        invocation_ts: float = 0.0,
        *,
        is_host_process: bool = False,
    ) -> bool:
        return self._registry.require(runtime_id).register_process(
            proc,
            container_name,
            invocation_ts,
            is_host_process=is_host_process,
        )

    def acquire_host_process(self, target: RuntimeTarget) -> HostProcessLease:
        self._next_host_process_generation += 1
        return self._registry.bind(target).acquire_host_process(self._next_host_process_generation)

    def register_host_process(
        self,
        lease: HostProcessLease,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        invocation_ts: float = 0.0,
    ) -> bool:
        return self._registry.require(lease.runtime_id).register_host_process(
            lease,
            proc,
            container_name,
            invocation_ts,
        )

    def release_host_process(self, lease: HostProcessLease) -> bool:
        state = self._registry.require(lease.runtime_id)
        if state.host_process_lease == lease and state.is_host_process and state.invocation_ts:
            self._container_runtime.destroy_gate(state.target.folder, state.invocation_ts)
        return state.release_host_process(lease)

    def defer_interrupt_until_tool_result(self, runtime_id: RuntimeId) -> None:
        state = self._registry.get(runtime_id)
        if state is not None and state.active:
            state.defer_interrupt_until_tool_result = True

    def claim_deferred_interrupt(self, runtime_id: RuntimeId) -> bool:
        """Mark a requested tool-boundary interrupt ready for process control."""
        state = self._registry.get(runtime_id)
        if state is None or not state.defer_interrupt_until_tool_result:
            return False
        state.defer_interrupt_until_tool_result = False
        state.boundary_interrupt_requested = True
        return True

    def boundary_interrupt_requested(self, runtime_id: RuntimeId) -> bool:
        state = self._registry.get(runtime_id)
        return state.boundary_interrupt_requested if state is not None else False

    def has_active_run(self, runtime_id: RuntimeId) -> bool:
        state = self._registry.get(runtime_id)
        if state is None:
            return False
        return state.active or (state.process is not None and state.process.returncode is None)

    def has_active_host_process(self, group_folder: str) -> bool:
        return any(
            state.active and state.is_host_process and state.target.folder == group_folder
            for state in self._registry.values()
        )

    def send_message(self, runtime_id: RuntimeId, text: str) -> bool:
        """Send best-effort context to an active scheduled-task container.

        Interactive follow-ups need their own output handler and query ID, so
        the queue serializes them as a separate warm turn instead of writing raw IPC.
        """
        state = self._registry.get(runtime_id)
        if state is None or not state.active or not state.active_is_task or state.is_host_process:
            return False
        try:
            self._container_runtime.write_message(state.target.folder, text)
        except OSError as exc:
            logger.warning(
                "Failed to write IPC message to container",
                runtime_id=runtime_id,
                err=str(exc),
            )
            return False
        return True

    def close_stdin(self, runtime_id: RuntimeId) -> None:
        """Signal the active container to wind down through IPC."""
        state = self._registry.get(runtime_id)
        if state is None or not state.active:
            return
        try:
            self._container_runtime.write_close_sentinel(state.target.folder)
        except OSError as exc:
            logger.warning(
                "Failed to write close sentinel to container",
                runtime_id=runtime_id,
                err=str(exc),
            )

    def clean_runtime_input(self, runtime_id: RuntimeId) -> None:
        """Discard IPC input that a completed runtime did not consume."""
        state = self._registry.require(runtime_id)
        self._container_runtime.clean_input_dir(state.target.folder)

    async def destroy_runtime_session(self, runtime_id: RuntimeId) -> None:
        """Remove the container session for a runtime, even after its process exits."""
        state = self._registry.get(runtime_id)
        group_folder = state.target.folder if state is not None else str(runtime_id)
        await self._container_runtime.destroy_session(group_folder)

    async def stop_active_process(self, runtime_id: RuntimeId) -> None:
        """Stop the disposable worker attached to a runtime."""
        state = self._registry.get(runtime_id)
        if state is None:
            return

        proc = state.process
        if state.is_host_process:
            if state.active and proc is not None:
                await stop_host_process(proc)
            return

        await self.destroy_runtime_session(runtime_id)
        if not state.active:
            return

        self.close_stdin(runtime_id)
        container_name = state.container_name
        if proc and container_name and proc.returncode is None:
            await self._container_runtime.graceful_stop(proc, container_name)

    async def stop_active_process_for_control(self, runtime_id: RuntimeId) -> None:
        """Mark and stop a turn controlled by a human command."""
        state = self._registry.get(runtime_id)
        if state is None:
            return
        state.boundary_interrupt_requested = True
        await self.stop_active_process(runtime_id)

    async def shutdown(self, *, active_count: int) -> None:
        """Destroy durable workers and stop any remaining runner processes."""
        logger.info(
            "GroupQueue shutdown starting",
            active_groups=len(self._registry.states),
            active_count=active_count,
        )
        await self._container_runtime.destroy_all_sessions()
        active: list[tuple[asyncio.subprocess.Process, str, bool]] = []
        for state in self._registry.values():
            proc_alive = getattr(state.process, "returncode", None) is None
            if state.process and state.container_name and proc_alive:
                active.append((state.process, state.container_name, state.is_host_process))
        if not active:
            logger.info("GroupQueue shutdown complete (no active containers)")
            return
        logger.info(
            "GroupQueue shutting down, stopping containers",
            active_count=len(active),
            containers=[name for _, name, _ in active],
        )
        await asyncio.gather(
            *(
                stop_host_process(proc)
                if is_host_process
                else self._container_runtime.graceful_stop(proc, name)
                for proc, name, is_host_process in active
            ),
            return_exceptions=True,
        )
        logger.info("GroupQueue shutdown complete", stopped_count=len(active))
