"""Serialize awaited work by runtime and enforce the host concurrency limit."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import (  # noqa: TC003 - beartype resolves queue annotations at runtime.
    Awaitable,
    Callable,
    Coroutine,
)
from typing import TypeVar
from uuid import uuid4

from pynchy.async_tasks import create_background_task
from pynchy.host.orchestrator.queue_serialization import await_queued_task
from pynchy.host.orchestrator.queue_state import (  # noqa: TC001 - beartype resolves queue annotations.
    HostProcessLease,
    QueuedTask,
)
from pynchy.host.orchestrator.runtime_process_control import (
    ContainerRuntimeOperations,
    RuntimeProcessControl,
)
from pynchy.host.orchestrator.runtime_registry import RuntimeRegistry
from pynchy.identifiers import (
    RuntimeId,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import (
    RuntimeTarget,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

_ResultT = TypeVar("_ResultT")


class GroupQueue:
    """Serialize every work source that targets the same execution runtime.

    Enforces a global concurrency limit across all runtimes. Messages take
    priority over scheduled tasks when draining, since a human is waiting.
    """

    def __init__(self, max_concurrent: int, container_runtime: ContainerRuntimeOperations) -> None:
        self._max_concurrent = max_concurrent
        self._registry = RuntimeRegistry()
        self._processes = RuntimeProcessControl(self._registry, container_runtime)
        self._active_count = 0
        self._waiting_groups: deque[RuntimeId] = deque()
        self._process_messages_fn: Callable[[str], Awaitable[TurnOutcome]] | None = None
        self._policy_paused: set[RuntimeId] = set()
        self._policy_boundaries: dict[RuntimeId, asyncio.Event] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._shutting_down = False

    def _start_background_task(self, coro: Coroutine[object, object, None], *, name: str) -> None:
        task = create_background_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[TurnOutcome]]) -> None:
        """Register the callback that processes pending messages for a group."""
        self._process_messages_fn = fn

    def _enqueue_task(self, target: RuntimeTarget, task: QueuedTask) -> bool:
        if self._shutting_down:
            return False
        state = self._registry.bind(target)
        if (state.active_task is not None and state.active_task.id == task.id) or any(
            pending.id == task.id for pending in state.pending_tasks
        ):
            return False
        state.pending_tasks.append(task)
        self._admit_pending(target.id)
        return True

    def _admit_pending(self, runtime_id: RuntimeId) -> None:
        state = self._registry.require(runtime_id)
        if state.active or runtime_id in self._policy_paused or not state.pending_tasks:
            return
        if self._active_count < self._max_concurrent:
            self._start_next_pending(runtime_id)
        elif runtime_id not in self._waiting_groups:
            self._waiting_groups.append(runtime_id)

    async def run_serialized_task(
        self,
        target: RuntimeTarget,
        task_id: str,
        fn: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        async def run_and_clean() -> _ResultT:
            try:
                return await fn()
            finally:
                # Discard unread forwarded IPC before slot reuse. Cleanup
                # failures belong to the caller and must not prevent release.
                self._processes.clean_runtime_input(target.id)

        return await await_queued_task(
            self._enqueue_task,
            self._cancel_queued_task,
            self.stop_active_process,
            target,
            task_id,
            run_and_clean,
        )

    def _cancel_queued_task(self, runtime_id: RuntimeId, task: QueuedTask) -> bool:
        """Remove queued work whose awaiting owner was cancelled."""
        state = self._registry.require(runtime_id)
        for pending in state.pending_tasks:
            if pending is not task:
                continue
            state.pending_tasks.remove(pending)
            return True
        return False

    async def run_message_turn(self, target: RuntimeTarget) -> TurnOutcome:
        """Run one interactive activity; its workflow owns retries and follow-ups."""
        if self._shutting_down:
            raise RuntimeError("Thread queue is shutting down")

        async def process() -> TurnOutcome:
            if self._process_messages_fn is None:
                return TurnOutcome.RETRY
            return await self._process_messages_fn(target.chat_jid)

        return await await_queued_task(
            self._enqueue_task,
            self._cancel_queued_task,
            self.stop_active_process,
            target,
            f"interactive-{uuid4().hex}",
            process,
            is_interactive=True,
        )

    def register_process(
        self,
        runtime_id: RuntimeId,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        invocation_ts: float = 0.0,
        *,
        is_host_process: bool = False,
    ) -> bool:
        return self._processes.register_process(
            runtime_id,
            proc,
            container_name,
            invocation_ts,
            is_host_process=is_host_process,
        )

    def acquire_host_process(self, target: RuntimeTarget) -> HostProcessLease:
        return self._processes.acquire_host_process(target)

    def register_host_process(
        self,
        lease: HostProcessLease,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        invocation_ts: float = 0.0,
    ) -> bool:
        return self._processes.register_host_process(
            lease,
            proc,
            container_name,
            invocation_ts,
        )

    def release_host_process(self, lease: HostProcessLease) -> bool:
        pending = self._processes.release_host_process(lease)
        state = self._registry.get(lease.runtime_id)
        if state is not None and not state.active:
            if lease.runtime_id in self._policy_paused:
                self._policy_boundaries[lease.runtime_id].set()
            else:
                self._admit_pending(lease.runtime_id)
        return pending

    def defer_interrupt_until_tool_result(self, runtime_id: RuntimeId) -> None:
        self._processes.defer_interrupt_until_tool_result(runtime_id)

    async def interrupt_after_tool_result(self, runtime_id: RuntimeId) -> bool:
        if not self._processes.claim_deferred_interrupt(runtime_id):
            return False
        await self.stop_active_process(runtime_id)
        return True

    def boundary_interrupt_requested(self, runtime_id: RuntimeId) -> bool:
        return self._processes.boundary_interrupt_requested(runtime_id)

    def is_active_task(self, runtime_id: RuntimeId) -> bool:
        """Check whether a scheduled task owns this runtime's active slot."""
        state = self._registry.get(runtime_id)
        return bool(state is not None and state.active and state.active_is_task)

    def has_activity(self, runtime_id: RuntimeId) -> bool:
        """Return whether a runtime has active or queued work."""
        state = self._registry.get(runtime_id)
        return self._registry.has_activity(state) if state is not None else False

    def has_active_run(self, runtime_id: RuntimeId) -> bool:
        return self._processes.has_active_run(runtime_id)

    def has_active_host_process(self, group_folder: str) -> bool:
        return self._processes.has_active_host_process(group_folder)

    def active_folders(self) -> set[str]:
        """Return folders whose runtime currently owns an execution slot."""
        return {state.target.folder for state in self._registry.values() if state.active}

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return a read-only snapshot of queue state for status reporting.

        Returns a dict keyed by runtime ID, each containing the runtime's current
        control binding and activity state, plus ``_meta`` global counters.
        """
        per_group: dict[str, dict[str, object]] = {}
        for runtime_id, state in self._registry.states.items():
            per_group[runtime_id] = {
                "chat_jid": state.target.chat_jid,
                "folder": state.target.folder,
                "active": state.active,
                "is_task": state.active_is_task,
                "pending_messages": state.pending_messages,
                "pending_tasks": sum(not task.is_interactive for task in state.pending_tasks),
            }
        per_group["_meta"] = {
            "active_count": self._active_count,
            "waiting_count": len(self._waiting_groups),
        }
        return per_group

    def send_message(self, runtime_id: RuntimeId, text: str) -> bool:
        return self._processes.send_message(runtime_id, text)

    def close_stdin(self, runtime_id: RuntimeId) -> None:
        self._processes.close_stdin(runtime_id)

    async def stop_active_process(self, runtime_id: RuntimeId) -> None:
        await self._processes.stop_active_process(runtime_id)

    async def destroy_runtime_session(self, runtime_id: RuntimeId) -> None:
        await self._processes.destroy_runtime_session(runtime_id)

    def is_runtime_policy_paused(self, runtime_id: RuntimeId) -> bool:  # noqa: V105
        """Return whether policy publication currently blocks this runtime."""
        return runtime_id in self._policy_paused

    async def pause_runtime_policy(self, targets: tuple[RuntimeTarget, ...]) -> None:
        """Block admissions and wait for each runtime's current turn boundary."""
        runtime_ids = tuple(target.id for target in targets)
        duplicate = self._policy_paused.intersection(runtime_ids)
        if duplicate:
            raise RuntimeError(f"Runtime policy refresh already active for {sorted(duplicate)!r}")

        events: list[asyncio.Event] = []
        registered: list[RuntimeId] = []
        try:
            for target in targets:
                event = self._pause_runtime_target(target)
                registered.append(target.id)
                events.append(event)
            await asyncio.gather(*(event.wait() for event in events))
        except BaseException:
            self.resume_runtime_policy(tuple(registered))
            raise

    def _pause_runtime_target(self, target: RuntimeTarget) -> asyncio.Event:
        state = self._registry.bind(target)
        event = asyncio.Event()
        self._policy_paused.add(target.id)
        self._policy_boundaries[target.id] = event
        if not state.active:
            event.set()
        return event

    def resume_runtime_policy(self, runtime_ids: tuple[RuntimeId, ...]) -> None:
        """Resume admissions after publishing one coherent policy."""
        for runtime_id in runtime_ids:
            self._policy_paused.discard(runtime_id)
            self._policy_boundaries.pop(runtime_id, None)
            while runtime_id in self._waiting_groups:
                self._waiting_groups.remove(runtime_id)
            state = self._registry.get(runtime_id)
            if state is None or state.active:
                continue
            self._admit_pending(runtime_id)
        self._drain_waiting()

    async def stop_active_process_for_control(self, runtime_id: RuntimeId) -> None:
        await self._processes.stop_active_process_for_control(runtime_id)

    def clear_pending_tasks(self, runtime_id: RuntimeId) -> tuple[str, ...]:
        """Cancel autonomous work while retaining queued interactive input."""
        return self._clear_pending(runtime_id, is_interactive=False)

    def clear_pending_messages(self, runtime_id: RuntimeId) -> None:
        """Cancel interactive activities invalidated outside the queue."""
        self._clear_pending(runtime_id, is_interactive=True)

    def _clear_pending(self, runtime_id: RuntimeId, *, is_interactive: bool) -> tuple[str, ...]:
        state = self._registry.get(runtime_id)
        if state is None:
            return ()
        cancelled = tuple(
            task for task in state.pending_tasks if task.is_interactive is is_interactive
        )
        for task in cancelled:
            state.pending_tasks.remove(task)
            task.cancel()
        return tuple(task.id for task in cancelled)

    async def _run_task(self, runtime_id: RuntimeId, task: QueuedTask) -> None:
        state = self._registry.require(runtime_id)
        try:
            await task.fn()
        finally:
            state.release()
            self._active_count -= 1
            self._drain_runtime(runtime_id)

    def _start_next_pending(self, runtime_id: RuntimeId) -> bool:
        """Start one item, preserving FIFO within each priority."""
        state = self._registry.require(runtime_id)
        if runtime_id in self._policy_paused or state.active or not state.pending_tasks:
            return False
        task = next(
            (item for item in state.pending_tasks if item.is_interactive),
            state.pending_tasks[0],
        )
        state.pending_tasks.remove(task)
        state.active = True
        state.active_task = task
        self._active_count += 1
        self._start_background_task(
            self._run_task(runtime_id, task), name=f"run-task-{task.id[:20]}"
        )
        return True

    def _drain_runtime(self, runtime_id: RuntimeId) -> None:
        """After a run finishes, start the next pending item for this runtime.

        If nothing is pending for this runtime, drain the global waiting queue.
        """
        if self._shutting_down:
            return

        if runtime_id in self._policy_paused:
            self._policy_boundaries[runtime_id].set()
            self._drain_waiting()
            return

        if not self._start_next_pending(runtime_id):
            self._drain_waiting()

    def _drain_waiting(self) -> None:
        """Start runs for waiting groups until the concurrency limit is hit."""
        while self._waiting_groups and self._active_count < self._max_concurrent:
            runtime_id = self._waiting_groups.popleft()
            self._start_next_pending(runtime_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        for state in self._registry.values():
            while state.pending_tasks:
                state.pending_tasks.popleft().cancel()
        await self._processes.shutdown(active_count=self._active_count)
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
