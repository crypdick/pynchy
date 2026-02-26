"""Per-group concurrency queue with global limits.

asyncio.ensure_future doesn't run the coroutine
synchronously up to the first await (unlike JS promises). So we must eagerly
set state.active and bump active_count in the synchronous caller, then clean
up in the async finally block.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pynchy.config import get_settings
from pynchy.ipc._write import write_ipc_close_sentinel, write_ipc_message
from pynchy.logger import logger
from pynchy.security.middleware import PolicyDeniedError


@dataclass
class QueuedTask:
    id: str
    group_jid: str
    fn: Callable[[], Awaitable[None]]


@dataclass
class GroupState:
    active: bool = False
    active_is_task: bool = False  # True when active container is a scheduled task
    pending_messages: bool = False
    pending_tasks: deque[QueuedTask] = field(default_factory=deque)
    process: asyncio.subprocess.Process | None = None
    container_name: str | None = None
    group_folder: str | None = None
    retry_count: int = 0

    def release(self) -> None:
        """Reset transient per-run state when a container slot is freed."""
        self.active = False
        self.active_is_task = False
        self.process = None
        self.container_name = None
        self.group_folder = None


class GroupQueue:
    """Per-group concurrency queue that serializes container runs within each group.

    Enforces a global concurrency limit across all groups. Messages take
    priority over scheduled tasks when draining, since a human is waiting.
    """

    def __init__(self) -> None:
        self._groups: dict[str, GroupState] = {}
        self._active_count = 0
        self._waiting_groups: deque[str] = deque()
        self._process_messages_fn: Callable[[str], Awaitable[bool]] | None = None
        self._shutting_down = False

    def _get_group(self, group_jid: str) -> GroupState:
        """Return the GroupState for *group_jid*, creating one if needed."""
        if group_jid not in self._groups:
            self._groups[group_jid] = GroupState()
        return self._groups[group_jid]

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        """Register the callback used to process pending messages for a group."""
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
        asyncio.ensure_future(self._run_for_group(group_jid, "messages"))

    def enqueue_task(self, group_jid: str, task_id: str, fn: Callable[[], Awaitable[None]]) -> None:
        """Queue a scheduled task for *group_jid*.

        Deduplicates by *task_id* — if the same task is already queued it is
        silently skipped.  Respects the same concurrency and per-group
        serialization rules as ``enqueue_message_check``.
        """
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # Prevent double-queuing of the same task
        if any(t.id == task_id for t in state.pending_tasks):
            logger.debug(
                "Task already queued, skipping",
                group_jid=group_jid,
                task_id=task_id,
            )
            return

        if state.active:
            state.pending_tasks.append(QueuedTask(id=task_id, group_jid=group_jid, fn=fn))
            logger.debug(
                "Container active, task queued",
                group_jid=group_jid,
                task_id=task_id,
            )
            return

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
            return

        # Eagerly mark as active before scheduling
        state.active = True
        state.active_is_task = True
        self._active_count += 1
        asyncio.ensure_future(
            self._run_task(group_jid, QueuedTask(id=task_id, group_jid=group_jid, fn=fn))
        )

    def register_process(
        self,
        group_jid: str,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        group_folder: str | None = None,
    ) -> None:
        """Associate a running container process with a group.

        Called by ``run_container_agent`` so the queue can stop the container
        on interrupts, send IPC messages, and track liveness.
        """
        state = self._get_group(group_jid)
        state.process = proc
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder

    def is_active_task(self, group_jid: str) -> bool:
        """Check if the active container for this group is a scheduled task."""
        state = self._get_group(group_jid)
        return state.active and state.active_is_task

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

        try:
            write_ipc_message(state.group_folder, text)
            return True
        except OSError as exc:
            logger.warning(
                "Failed to write IPC message to container",
                group_jid=group_jid,
                err=str(exc),
            )
            return False

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
        _close sentinel and calls ``docker stop`` with a 15s fallback to kill.
        """
        state = self._get_group(group_jid)

        # Destroy persistent session (handles its own graceful stop + docker rm)
        if state.group_folder:
            from pynchy.container_runner._session import destroy_session

            await destroy_session(state.group_folder)

        if not state.active:
            return

        # Cooperative signal first
        self.close_stdin(group_jid)

        # Force-stop the container process (for one-shot containers without sessions)
        proc = state.process
        container_name = state.container_name
        if proc and container_name and proc.returncode is None:
            from pynchy.container_runner._process import _graceful_stop

            await _graceful_stop(proc, container_name)

    def clear_pending_tasks(self, group_jid: str) -> None:
        """Drop all pending tasks for a group."""
        state = self._get_group(group_jid)
        state.pending_tasks.clear()

    @staticmethod
    def _cleanup_ipc_input(group_folder: str | None) -> None:
        """Remove stale IPC input files left by best-effort "btw " delivery.

        Called after a task container exits so the next container (started
        by _drain_group) doesn't ingest duplicates from both the SDK
        message list and leftover IPC files.
        """
        if not group_folder:
            return
        input_dir = get_settings().data_dir / "ipc" / group_folder / "input"
        if not input_dir.is_dir():
            return
        for f in input_dir.iterdir():
            if f.suffix == ".json":
                with contextlib.suppress(OSError):
                    f.unlink()

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
            if self._process_messages_fn:
                success = await self._process_messages_fn(group_jid)
                if success:
                    state.retry_count = 0
                else:
                    self._schedule_retry(group_jid, state)
        except PolicyDeniedError as exc:
            # Deterministic failure — retrying won't change the outcome
            logger.warning(
                "Policy denial for group, not retrying",
                group_jid=group_jid,
                err=str(exc),
            )
        except Exception:
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
        except Exception:
            logger.exception(
                "Error running task",
                group_jid=group_jid,
                task_id=task.id,
            )
        finally:
            # Clean up stale IPC input files before drain may start a
            # new container — prevents the next container from seeing
            # duplicates of "btw " messages that were best-effort
            # forwarded but never read by the now-dead task container.
            self._cleanup_ipc_input(state.group_folder)
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

        asyncio.ensure_future(_retry())

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
            asyncio.ensure_future(self._run_for_group(group_jid, "drain"))
            return True

        if state.pending_tasks:
            task = state.pending_tasks.popleft()
            state.active = True
            state.active_is_task = True
            self._active_count += 1
            asyncio.ensure_future(self._run_task(group_jid, task))
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
        from pynchy.container_runner._session import destroy_all_sessions

        await destroy_all_sessions()

        # Stop any remaining one-shot containers
        active: list[tuple[asyncio.subprocess.Process, str]] = []
        for _jid, state in self._groups.items():
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

        from pynchy.container_runner._process import _graceful_stop

        await asyncio.gather(
            *(_graceful_stop(proc, name) for proc, name in active),
            return_exceptions=True,
        )
        logger.info(
            "GroupQueue shutdown complete",
            stopped_count=len(active),
        )
