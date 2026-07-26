"""Process lifecycle and IPC control for queued execution runtimes."""

from __future__ import annotations

import asyncio  # noqa: TC003, RUF100 - beartype resolves process annotations.

import pynchy.host.container_manager.process as container_process
import pynchy.host.container_manager.session as container_session
from pynchy.host.container_manager.ipc.write import (
    write_ipc_close_sentinel,
    write_ipc_message,
)
from pynchy.host.orchestrator.host_runner import stop_host_process
from pynchy.host.orchestrator.queue_state import HostProcessLease  # noqa: TC001, RUF100
from pynchy.host.orchestrator.runtime_registry import (  # noqa: TC001, RUF100 - beartype resolves controller annotations.
    RuntimeRegistry,
)
from pynchy.host.orchestrator.runtime_target import RuntimeTarget  # noqa: TC001, RUF100
from pynchy.logger import logger
from pynchy.types import RuntimeId  # noqa: TC001, RUF100


class RuntimeProcessControl:
    """Attach, observe, interrupt, and release runtime worker processes."""

    def __init__(self, registry: RuntimeRegistry) -> None:
        self._registry = registry
        self._next_host_process_generation = 0

    def register_process(
        self,
        runtime_id: RuntimeId,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        invocation_ts: float = 0.0,
        *,
        is_host_process: bool = False,
    ) -> None:
        self._registry.require(runtime_id).register_process(
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
        return self._registry.require(lease.runtime_id).release_host_process(lease)

    def defer_interrupt_until_tool_result(self, runtime_id: RuntimeId) -> None:
        state = self._registry.get(runtime_id)
        if state is not None and state.active:
            state.defer_interrupt_until_tool_result = True
            state.pending_messages = True

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
        """Send a follow-up message to the active container through IPC."""
        state = self._registry.get(runtime_id)
        if state is None or not state.active or state.is_host_process:
            return False
        try:
            write_ipc_message(state.target.folder, text)
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
            write_ipc_close_sentinel(state.target.folder)
        except OSError as exc:
            logger.warning(
                "Failed to write close sentinel to container",
                runtime_id=runtime_id,
                err=str(exc),
            )

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

        await container_session.destroy_session(state.target.folder)
        if not state.active:
            return

        self.close_stdin(runtime_id)
        container_name = state.container_name
        if proc and container_name and proc.returncode is None:
            await container_process.graceful_stop(proc, container_name)

    async def stop_active_process_for_control(self, runtime_id: RuntimeId) -> None:
        """Mark and stop a turn controlled by a human command."""
        state = self._registry.get(runtime_id)
        if state is None:
            return
        state.boundary_interrupt_requested = True
        await self.stop_active_process(runtime_id)
