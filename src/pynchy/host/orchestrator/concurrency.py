"""Per-runtime concurrency queue with global limits.

State (``active``, ``_active_count``) is set eagerly in the synchronous
enqueue methods so that a second synchronous call sees the correct state.
The async ``_run_*`` methods clean up in their ``finally`` blocks.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import (  # noqa: TC003 - beartype resolves queue annotations at runtime.
    Awaitable,
    Callable,
    Coroutine,
)
from dataclasses import dataclass
from typing import TypeVar

from pynchy.async_tasks import create_background_task
from pynchy.host.orchestrator.queue_serialization import (
    await_message_turn,
    await_queued_task,
)
from pynchy.host.orchestrator.queue_state import GroupState, HostProcessLease, QueuedTask
from pynchy.host.orchestrator.runtime_process_control import (
    ContainerRuntimeOperations,
    RuntimeProcessControl,
)
from pynchy.host.orchestrator.runtime_registry import RuntimeRegistry
from pynchy.identifiers import (
    RuntimeId,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.logger import logger
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import (
    RuntimeTarget,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, kw_only=True)
class QueuePolicy:
    """Resolved admission and retry limits for one runtime queue."""

    max_concurrent: int
    max_retries: int
    retry_base_seconds: float


class GroupQueue:
    """Serialize every work source that targets the same execution runtime.

    Enforces a global concurrency limit across all runtimes. Messages take
    priority over scheduled tasks when draining, since a human is waiting.
    """

    def __init__(self, policy: QueuePolicy, container_runtime: ContainerRuntimeOperations) -> None:
        self._policy = policy
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

    def enqueue_message_check(self, target: RuntimeTarget) -> None:
        """Schedule a message processing run for *target*.

        If the runtime is already active, the check is deferred until its current
        run finishes. If the global limit is reached, the runtime waits globally.
        """
        if self._shutting_down:
            return

        state = self._registry.bind(target)

        if target.id in self._policy_paused:
            state.pending_messages = True
            return

        if state.active:
            state.pending_messages = True
            logger.debug(
                "Runtime active, message queued",
                runtime_id=target.id,
                chat_jid=target.chat_jid,
            )
            return

        if self._active_count >= self._policy.max_concurrent:
            state.pending_messages = True
            if target.id not in self._waiting_groups:
                self._waiting_groups.append(target.id)
            logger.debug(
                "At concurrency limit, message queued",
                runtime_id=target.id,
                chat_jid=target.chat_jid,
                active_count=self._active_count,
            )
            return

        # Eagerly mark as active before scheduling the coroutine
        state.active = True
        state.pending_messages = False
        self._active_count += 1
        self._start_background_task(
            self._run_for_runtime(target.id, "messages"),
            name=f"process-messages-{target.id[:20]}",
        )

    def enqueue_task(
        self,
        target: RuntimeTarget,
        task_id: str,
        fn: Callable[[], Awaitable[None]],
    ) -> bool:
        return self._enqueue_task(target, QueuedTask(id=task_id, fn=fn))

    def _enqueue_task(self, target: RuntimeTarget, task: QueuedTask) -> bool:
        """Queue autonomous work for *target*.

        Deduplicates by logical task ID — if the same task is active or queued it is
        silently skipped.  Respects the same concurrency and per-group
        serialization rules as ``enqueue_message_check``.

        Returns True when the task was accepted for immediate or pending
        execution, False when the queue rejected it.
        """
        if self._shutting_down:
            return False

        state = self._registry.bind(target)

        if (state.active_task is not None and state.active_task.id == task.id) or any(
            pending.id == task.id for pending in state.pending_tasks
        ):
            logger.debug(
                "Task already queued, skipping",
                runtime_id=target.id,
                task_id=task.id,
            )
            return False

        if target.id in self._policy_paused:
            state.pending_tasks.append(task)
            return True

        if state.active:
            state.pending_tasks.append(task)
            logger.debug(
                "Runtime active, task queued",
                runtime_id=target.id,
                task_id=task.id,
            )
            return True

        if self._active_count >= self._policy.max_concurrent:
            state.pending_tasks.append(task)
            if target.id not in self._waiting_groups:
                self._waiting_groups.append(target.id)
            logger.debug(
                "At concurrency limit, task queued",
                runtime_id=target.id,
                task_id=task.id,
                active_count=self._active_count,
            )
            return True

        # Eagerly mark as active before scheduling
        state.active = True
        state.active_is_task = True
        state.active_task = task
        self._active_count += 1
        self._start_background_task(
            self._run_task(target.id, task),
            name=f"run-task-{task.id[:20]}",
        )
        return True

    async def run_serialized_task(
        self,
        target: RuntimeTarget,
        task_id: str,
        fn: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        return await await_queued_task(
            self._enqueue_task,
            self._cancel_queued_task,
            self.stop_active_process,
            target,
            task_id,
            fn,
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
        return await await_message_turn(
            self._registry.bind,
            self.enqueue_message_check,
            target,
            shutting_down=self._shutting_down,
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
        if lease.runtime_id in self._policy_paused and state is not None and not state.active:
            self._policy_boundaries[lease.runtime_id].set()
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
                "pending_tasks": len(state.pending_tasks),
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

    def is_runtime_policy_paused(self, runtime_id: RuntimeId) -> bool:
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
            if self._active_count < self._policy.max_concurrent:
                self._start_next_pending(runtime_id)
            elif (
                state.pending_messages or state.pending_tasks
            ) and runtime_id not in self._waiting_groups:
                self._waiting_groups.append(runtime_id)
        self._drain_waiting()

    async def stop_active_process_for_control(self, runtime_id: RuntimeId) -> None:
        await self._processes.stop_active_process_for_control(runtime_id)

    def clear_pending_tasks(self, runtime_id: RuntimeId) -> tuple[str, ...]:
        state = self._registry.get(runtime_id)
        return self._cancel_pending_tasks(state) if state is not None else ()

    def clear_pending_messages(self, runtime_id: RuntimeId) -> None:
        """Drop message work invalidated outside the queue."""
        state = self._registry.get(runtime_id)
        if state is not None:
            state.pending_messages = False
            state.cancel_message_waiters()

    @staticmethod
    def _cancel_pending_tasks(state: GroupState) -> tuple[str, ...]:
        task_ids = tuple(task.id for task in state.pending_tasks)
        while state.pending_tasks:
            state.pending_tasks.popleft().cancel()
        return task_ids

    async def _process_group_messages(
        self,
        state: GroupState,
    ) -> TurnOutcome:
        """Process messages for a runtime and schedule retry on failure."""
        if not self._process_messages_fn:
            return TurnOutcome.RETRY

        result = await self._process_messages_fn(state.target.chat_jid)
        if result in {
            TurnOutcome.COMPLETED,
            TurnOutcome.PAUSED,
            TurnOutcome.RESET,
        }:
            state.retry_count = 0
        elif result is TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT:
            state.retry_count = 0
            state.pending_messages = True
        else:
            self._schedule_retry(state)
        return result

    async def _run_for_runtime(self, runtime_id: RuntimeId, reason: str) -> None:
        """Run the message processor for one runtime.

        State is already marked active by the caller (enqueue_message_check
        or drain). We only clean up in finally.
        """
        state = self._registry.require(runtime_id)

        logger.debug(
            "Starting agent for runtime",
            runtime_id=runtime_id,
            chat_jid=state.target.chat_jid,
            reason=reason,
            active_count=self._active_count,
        )

        result = TurnOutcome.RETRY
        error: BaseException | None = None
        try:
            result = await self._process_group_messages(state)
        except Exception:  # noqa: BLE001 - message-processing is a task boundary; retry happens on drain.
            error = RuntimeError(f"Error processing messages for runtime {runtime_id}")
            logger.exception(
                "Error processing messages for runtime",
                runtime_id=runtime_id,
            )
            self._schedule_retry(state)
        finally:
            waiters, state.message_waiters = state.message_waiters, []
            for waiter in waiters:
                if waiter.done():
                    continue
                if error is not None:
                    waiter.set_exception(error)
                else:
                    waiter.set_result(result)
            state.release()
            self._active_count -= 1
            self._drain_runtime(runtime_id)

    async def _run_task(self, runtime_id: RuntimeId, task: QueuedTask) -> None:
        """Run a queued task.

        State is already marked active by the caller.
        """
        state = self._registry.require(runtime_id)

        logger.debug(
            "Running queued task",
            runtime_id=runtime_id,
            task_id=task.id,
            active_count=self._active_count,
        )

        try:
            await task.fn()
        except Exception:  # noqa: BLE001 - task execution is a queue boundary and failures stay scoped.
            logger.exception(
                "Error running task",
                runtime_id=runtime_id,
                task_id=task.id,
            )
        finally:
            # Clean up stale IPC input files before drain may start a
            # container — prevents the next container from seeing
            # duplicates of "btw " messages that were best-effort
            # forwarded but never read by the now-dead task container.
            self._processes.clean_runtime_input(runtime_id)
            state.release()
            self._active_count -= 1
            self._drain_runtime(runtime_id)

    def _schedule_retry(self, state: GroupState) -> None:
        """Re-enqueue a failed message check after exponential backoff."""
        runtime_id = state.target.id
        state.retry_count += 1
        if state.retry_count > self._policy.max_retries:
            logger.error(
                "Max retries exceeded, dropping messages (will retry on next incoming message)",
                runtime_id=runtime_id,
                retry_count=state.retry_count,
            )
            state.retry_count = 0
            return

        delay = self._policy.retry_base_seconds * (2 ** (state.retry_count - 1))
        logger.info(
            "Scheduling retry with backoff",
            runtime_id=runtime_id,
            retry_count=state.retry_count,
            delay_seconds=delay,
        )

        async def _retry() -> None:
            await asyncio.sleep(delay)
            self.enqueue_message_check(state.target)

        self._start_background_task(_retry(), name=f"retry-{runtime_id[:20]}")

    def _start_next_pending(self, runtime_id: RuntimeId) -> bool:
        """Try to start the next pending item for *runtime_id*.

        Messages are drained before tasks (human > autonomous priority).
        Returns True if work was started, False if the runtime has nothing pending.
        """
        state = self._registry.require(runtime_id)
        if runtime_id in self._policy_paused:
            return False

        if state.pending_messages:
            state.active = True
            state.active_is_task = False
            state.pending_messages = False
            self._active_count += 1
            self._start_background_task(
                self._run_for_runtime(runtime_id, "drain"),
                name=f"drain-messages-{runtime_id[:20]}",
            )
            return True

        if state.pending_tasks:
            task = state.pending_tasks.popleft()
            state.active = True
            state.active_is_task = True
            state.active_task = task
            self._active_count += 1
            self._start_background_task(
                self._run_task(runtime_id, task),
                name=f"drain-task-{task.id[:20]}",
            )
            return True

        return False

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
        while self._waiting_groups and self._active_count < self._policy.max_concurrent:
            runtime_id = self._waiting_groups.popleft()
            self._start_next_pending(runtime_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        for state in self._registry.values():
            self._cancel_pending_tasks(state)
            state.cancel_message_waiters()
        await self._processes.shutdown(active_count=self._active_count)
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
