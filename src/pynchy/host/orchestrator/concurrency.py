"""Per-group concurrency queue with global limits.

State (``active``, ``_active_count``) is set eagerly in the synchronous
enqueue methods so that a second synchronous call sees the correct state.
The async ``_run_*`` methods clean up in their ``finally`` blocks.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves queue annotations at runtime.
    Awaitable,
    Callable,
)

import pynchy.host.container_manager.process as container_process
import pynchy.host.container_manager.session as container_session
from pynchy.config import get_settings
from pynchy.host.container_manager.ipc.write import (
    clean_ipc_input_dir,
    write_ipc_close_sentinel,
    write_ipc_message,
)
from pynchy.host.container_manager.security.middleware import PolicyDeniedError
from pynchy.host.orchestrator.host_runner import stop_host_process
from pynchy.host.orchestrator.messaging.outcomes import (
    CONTINUE_AFTER_SAFE_INTERRUPT,
    ProcessGroupResult,
)
from pynchy.host.orchestrator.queue_state import GroupState, HostProcessLease, QueuedTask
from pynchy.logger import logger
from pynchy.utils import create_background_task


class GroupQueue:
    """Per-group concurrency queue that serializes container runs within each group.

    Enforces a global concurrency limit across all groups. Messages take
    priority over scheduled tasks when draining, since a human is waiting.
    """

    def __init__(self) -> None:
        self._groups: dict[str, GroupState] = {}
        self._next_host_process_generation = 0
        self._active_count = 0
        self._waiting_groups: deque[str] = deque()
        self._process_messages_fn: Callable[[str], Awaitable[ProcessGroupResult]] | None = None
        self._shutting_down = False

    def _get_group(self, group_jid: str) -> GroupState:
        """Return the GroupState for *group_jid*, creating one if needed."""
        if group_jid not in self._groups:
            self._groups[group_jid] = GroupState()
        return self._groups[group_jid]

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[ProcessGroupResult]]) -> None:
        """Register the callback that processes pending messages for a group."""
        self._process_messages_fn = fn

    def enqueue_message_check(self, group_jid: str) -> None:
        """Schedule a message processing run for *group_jid*.

        If the group already has an active container, the check is deferred
        until the current run finishes.  If the global concurrency limit is
        reached, the group is added to the waiting queue.
        """
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        if state.active:
            state.pending_messages = True
            logger.debug("Container active, message queued", group_jid=group_jid)
            return

        if self._active_count >= get_settings().container.max_concurrent:
            state.pending_messages = True
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug(
                "At concurrency limit, message queued",
                group_jid=group_jid,
                active_count=self._active_count,
            )
            return

        # Eagerly mark as active before scheduling the coroutine
        state.active = True
        state.pending_messages = False
        self._active_count += 1
        create_background_task(
            self._run_for_group(group_jid, "messages"),
            name=f"process-messages-{group_jid[:20]}",
        )

    def enqueue_task(self, group_jid: str, task_id: str, fn: Callable[[], Awaitable[None]]) -> bool:
        """Queue a scheduled task for *group_jid*.

        Deduplicates by *task_id* — if the same task is already queued it is
        silently skipped.  Respects the same concurrency and per-group
        serialization rules as ``enqueue_message_check``.

        Returns True when the task was accepted for immediate or pending
        execution, False when the queue rejected it.
        """
        if self._shutting_down:
            return False

        state = self._get_group(group_jid)

        # Prevent double-queuing of the same task
        if any(t.id == task_id for t in state.pending_tasks):
            logger.debug(
                "Task already queued, skipping",
                group_jid=group_jid,
                task_id=task_id,
            )
            return False

        if state.active:
            state.pending_tasks.append(QueuedTask(id=task_id, group_jid=group_jid, fn=fn))
            logger.debug(
                "Container active, task queued",
                group_jid=group_jid,
                task_id=task_id,
            )
            return True

        if self._active_count >= get_settings().container.max_concurrent:
            state.pending_tasks.append(QueuedTask(id=task_id, group_jid=group_jid, fn=fn))
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug(
                "At concurrency limit, task queued",
                group_jid=group_jid,
                task_id=task_id,
                active_count=self._active_count,
            )
            return True

        # Eagerly mark as active before scheduling
        state.active = True
        state.active_is_task = True
        self._active_count += 1
        create_background_task(
            self._run_task(group_jid, QueuedTask(id=task_id, group_jid=group_jid, fn=fn)),
            name=f"run-task-{task_id[:20]}",
        )
        return True

    def register_process(  # noqa: PLR0913, RUF100 - process registration records all queue cleanup state.
        self,
        group_jid: str,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        group_folder: str | None = None,
        invocation_ts: float = 0.0,
        *,
        is_host_process: bool = False,
    ) -> None:
        """Associate a running container process with a group.

        Called by ``run_container_agent`` so the queue can stop the container
        on interrupts, send IPC messages, and track liveness.
        """
        state = self._get_group(group_jid)
        state.register_process(
            proc,
            container_name,
            group_folder,
            invocation_ts,
            is_host_process=is_host_process,
        )

    def acquire_host_process(self, group_jid: str) -> HostProcessLease:
        """Reserve queue state for one direct host process before it is spawned."""
        self._next_host_process_generation += 1
        return self._get_group(group_jid).acquire_host_process(
            group_jid,
            self._next_host_process_generation,
        )

    def register_host_process(  # noqa: PLR0913, RUF100 - records every host process attribute.
        self,
        lease: HostProcessLease,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        group_folder: str | None = None,
        invocation_ts: float = 0.0,
    ) -> bool:
        """Attach a spawned host process to its pre-acquired queue lease."""
        return self._get_group(lease.group_jid).register_host_process(
            lease,
            proc,
            container_name,
            group_folder,
            invocation_ts,
        )

    def release_host_process(self, lease: HostProcessLease) -> bool:
        """Release only the direct host process that owns *lease*."""
        return self._get_group(lease.group_jid).release_host_process(lease)

    def defer_interrupt_until_tool_result(self, group_jid: str) -> None:
        """Queue an active turn for interruption after its current tool completes."""
        state = self._get_group(group_jid)
        if state.active:
            state.defer_interrupt_until_tool_result = True
            state.pending_messages = True

    async def interrupt_after_tool_result(self, group_jid: str) -> bool:
        """Interrupt a queued active turn only after a completed tool event."""
        state = self._get_group(group_jid)
        if not state.defer_interrupt_until_tool_result:
            return False
        state.defer_interrupt_until_tool_result = False
        state.boundary_interrupt_requested = True
        await self.stop_active_process(group_jid)
        return True

    def boundary_interrupt_requested(self, group_jid: str) -> bool:
        """Whether the active host process was stopped at a tool boundary."""
        return self._get_group(group_jid).boundary_interrupt_requested

    def is_active_task(self, group_jid: str) -> bool:
        """Check if the active container for this group is a scheduled task."""
        state = self._get_group(group_jid)
        return state.active and state.active_is_task

    def has_active_host_process(self, group_folder: str) -> bool:
        """Return whether a direct host agent is waiting for this group's IPC."""
        return any(
            state.active and state.is_host_process and state.group_folder == group_folder
            for state in self._groups.values()
        )

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return a read-only snapshot of queue state for status reporting.

        Returns a dict keyed by group JID, each containing the group's
        active/pending state.  Also includes ``_meta`` with global counters.
        """
        per_group: dict[str, dict[str, object]] = {}
        for jid, state in self._groups.items():
            per_group[jid] = {
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

    def send_message(self, group_jid: str, text: str) -> bool:
        """Send a follow-up message to the active container via IPC file."""
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder:
            return False
        if state.is_host_process:
            # Host runners have no IPC watcher. Returning False leaves the
            # message pending for the next safe tool-result boundary instead.
            return False

        try:
            write_ipc_message(state.group_folder, text)
        except OSError as exc:
            logger.warning(
                "Failed to write IPC message to container",
                group_jid=group_jid,
                err=str(exc),
            )
            return False
        else:
            return True

    def close_stdin(self, group_jid: str) -> None:
        """Signal the active container to wind down by writing a close sentinel."""
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder:
            return

        try:
            write_ipc_close_sentinel(state.group_folder)
        except OSError as exc:
            logger.warning(
                "Failed to write close sentinel to container",
                group_jid=group_jid,
                err=str(exc),
            )

    async def stop_active_process(self, group_jid: str) -> None:
        """Force-stop the active container for a group.

        Destroys any persistent session first, then writes the cooperative
        _close sentinel and calls ``docker stop`` with a 15s timeout before killing.
        """
        state = self._get_group(group_jid)

        proc = state.process
        if state.is_host_process:
            if state.active and proc is not None:
                await stop_host_process(proc)
            return

        # Destroy persistent session (handles its own graceful stop + docker rm)
        if state.group_folder:
            await container_session.destroy_session(state.group_folder)

        if not state.active:
            return

        # Cooperative signal first
        self.close_stdin(group_jid)

        # Force-stop the container process (for one-shot containers without sessions)
        container_name = state.container_name
        if proc and container_name and proc.returncode is None:
            await container_process.graceful_stop(proc, container_name)

    def clear_pending_tasks(self, group_jid: str) -> None:
        """Drop all pending tasks for a group."""
        state = self._get_group(group_jid)
        state.pending_tasks.clear()

    async def _process_group_messages(self, group_jid: str, state: GroupState) -> None:
        """Process messages for a group and schedule retry on failure."""
        if not self._process_messages_fn:
            return

        result = await self._process_messages_fn(group_jid)
        if result is CONTINUE_AFTER_SAFE_INTERRUPT:
            state.retry_count = 0
            state.pending_messages = True
        elif result:
            state.retry_count = 0
        else:
            self._schedule_retry(group_jid, state)

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        """Run the process_messages_fn for a group.

        State is already marked active by the caller (enqueue_message_check
        or _drain_group). We only clean up in finally.
        """
        state = self._get_group(group_jid)

        logger.debug(
            "Starting container for group",
            group_jid=group_jid,
            reason=reason,
            active_count=self._active_count,
        )

        try:
            await self._process_group_messages(group_jid, state)
        except PolicyDeniedError as exc:
            # Deterministic failure — retrying won't change the outcome
            logger.warning(
                "Policy denial for group, not retrying",
                group_jid=group_jid,
                err=str(exc),
            )
        except Exception:  # noqa: BLE001, RUF100 - message-processing is a task boundary; retry happens on drain.
            logger.exception(
                "Error processing messages for group",
                group_jid=group_jid,
            )
            self._schedule_retry(group_jid, state)
        finally:
            state.release()
            self._active_count -= 1
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: QueuedTask) -> None:
        """Run a queued task.

        State is already marked active by the caller.
        """
        state = self._get_group(group_jid)

        logger.debug(
            "Running queued task",
            group_jid=group_jid,
            task_id=task.id,
            active_count=self._active_count,
        )

        try:
            await task.fn()
        except Exception:  # noqa: BLE001, RUF100 - task execution is a queue boundary and failures stay scoped.
            logger.exception(
                "Error running task",
                group_jid=group_jid,
                task_id=task.id,
            )
        finally:
            # Clean up stale IPC input files before drain may start a
            # container — prevents the next container from seeing
            # duplicates of "btw " messages that were best-effort
            # forwarded but never read by the now-dead task container.
            clean_ipc_input_dir(state.group_folder)
            state.release()
            self._active_count -= 1
            self._drain_group(group_jid)

    def _schedule_retry(self, group_jid: str, state: GroupState) -> None:
        """Re-enqueue a failed message check after exponential backoff."""
        s = get_settings()
        state.retry_count += 1
        if state.retry_count > s.queue.max_retries:
            logger.error(
                "Max retries exceeded, dropping messages (will retry on next incoming message)",
                group_jid=group_jid,
                retry_count=state.retry_count,
            )
            state.retry_count = 0
            return

        delay = s.queue.base_retry_seconds * (2 ** (state.retry_count - 1))
        logger.info(
            "Scheduling retry with backoff",
            group_jid=group_jid,
            retry_count=state.retry_count,
            delay_seconds=delay,
        )

        async def _retry() -> None:
            await asyncio.sleep(delay)
            if not self._shutting_down:
                self.enqueue_message_check(group_jid)

        create_background_task(_retry(), name=f"retry-{group_jid[:20]}")

    def _start_next_pending(self, group_jid: str) -> bool:
        """Try to start the next pending item for *group_jid*.

        Messages are drained before tasks (human > autonomous priority).
        Returns True if work was started, False if the group has nothing pending.
        """
        state = self._get_group(group_jid)

        if state.pending_messages:
            state.active = True
            state.active_is_task = False
            state.pending_messages = False
            self._active_count += 1
            create_background_task(
                self._run_for_group(group_jid, "drain"),
                name=f"drain-messages-{group_jid[:20]}",
            )
            return True

        if state.pending_tasks:
            task = state.pending_tasks.popleft()
            state.active = True
            state.active_is_task = True
            self._active_count += 1
            create_background_task(
                self._run_task(group_jid, task),
                name=f"drain-task-{task.id[:20]}",
            )
            return True

        return False

    def _drain_group(self, group_jid: str) -> None:
        """After a run finishes, start the next pending item for this group.

        If nothing is pending for this group, drains the global waiting queue.
        """
        if self._shutting_down:
            return

        if not self._start_next_pending(group_jid):
            self._drain_waiting()

    def _drain_waiting(self) -> None:
        """Start runs for waiting groups until the concurrency limit is hit."""
        while self._waiting_groups and self._active_count < get_settings().container.max_concurrent:
            next_jid = self._waiting_groups.popleft()
            self._start_next_pending(next_jid)

    async def shutdown(self) -> None:
        self._shutting_down = True
        logger.info(
            "GroupQueue shutdown starting",
            active_groups=len(self._groups),
            active_count=self._active_count,
        )

        # Destroy all persistent sessions first
        await container_session.destroy_all_sessions()

        # Stop any remaining one-shot containers
        active: list[tuple[asyncio.subprocess.Process, str]] = []
        for state in self._groups.values():
            proc_alive = getattr(state.process, "returncode", None) is None
            if state.process and state.container_name and proc_alive:
                active.append((state.process, state.container_name))

        if not active:
            logger.info("GroupQueue shutdown complete (no active containers)")
            return

        logger.info(
            "GroupQueue shutting down, stopping containers",
            active_count=len(active),
            containers=[name for _, name in active],
        )

        await asyncio.gather(
            *(container_process.graceful_stop(proc, name) for proc, name in active),
            return_exceptions=True,
        )
        logger.info(
            "GroupQueue shutdown complete",
            stopped_count=len(active),
        )
