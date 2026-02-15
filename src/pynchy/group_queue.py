"""Per-group concurrency queue with global limits.

Port of src/group-queue.ts — uses asyncio instead of Node.js event loop.

Key difference from TS: asyncio.ensure_future doesn't run the coroutine
synchronously up to the first await (unlike JS promises). So we must eagerly
set state.active and bump active_count in the synchronous caller, then clean
up in the async finally block.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pynchy.config import DATA_DIR, MAX_CONCURRENT_CONTAINERS
from pynchy.logger import logger

MAX_RETRIES = 5
BASE_RETRY_SECONDS = 5.0


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
    pending_tasks: list[QueuedTask] = field(default_factory=list)
    process: Any = None  # asyncio.subprocess.Process or similar
    container_name: str | None = None
    group_folder: str | None = None
    retry_count: int = 0


class GroupQueue:
    def __init__(self) -> None:
        self._groups: dict[str, GroupState] = {}
        self._active_count = 0
        self._waiting_groups: list[str] = []
        self._process_messages_fn: Callable[[str], Awaitable[bool]] | None = None
        self._shutting_down = False

    def _get_group(self, group_jid: str) -> GroupState:
        if group_jid not in self._groups:
            self._groups[group_jid] = GroupState()
        return self._groups[group_jid]

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self._process_messages_fn = fn

    def enqueue_message_check(self, group_jid: str) -> None:
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        if state.active:
            state.pending_messages = True
            logger.debug("Container active, message queued", group_jid=group_jid)
            return

        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
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

        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
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
        proc: Any,
        container_name: str,
        group_folder: str | None = None,
    ) -> None:
        state = self._get_group(group_jid)
        state.process = proc
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder

    def is_active_task(self, group_jid: str) -> bool:
        """Check if the active container for this group is a scheduled task."""
        state = self._get_group(group_jid)
        return state.active and state.active_is_task

    def send_message(self, group_jid: str, text: str) -> bool:
        """Send a follow-up message to the active container via IPC file."""
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder:
            return False

        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{int(time.time() * 1000)}-{random.randbytes(3).hex()}.json"
            filepath = input_dir / filename
            temp_path = filepath.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps({"type": "message", "text": text}))
            temp_path.rename(filepath)
            return True
        except Exception:
            return False

    def close_stdin(self, group_jid: str) -> None:
        """Signal the active container to wind down by writing a close sentinel."""
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder:
            return

        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / "_close").write_text("")
        except Exception:
            pass

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
        except Exception as exc:
            logger.error(
                "Error processing messages for group",
                group_jid=group_jid,
                err=str(exc),
            )
            self._schedule_retry(group_jid, state)
        finally:
            state.active = False
            state.active_is_task = False
            state.process = None
            state.container_name = None
            state.group_folder = None
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
        except Exception as exc:
            logger.error(
                "Error running task",
                group_jid=group_jid,
                task_id=task.id,
                err=str(exc),
            )
        finally:
            state.active = False
            state.active_is_task = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    def _schedule_retry(self, group_jid: str, state: GroupState) -> None:
        state.retry_count += 1
        if state.retry_count > MAX_RETRIES:
            logger.error(
                "Max retries exceeded, dropping messages (will retry on next incoming message)",
                group_jid=group_jid,
                retry_count=state.retry_count,
            )
            state.retry_count = 0
            return

        delay = BASE_RETRY_SECONDS * (2 ** (state.retry_count - 1))
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

    def _drain_group(self, group_jid: str) -> None:
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # Messages first — a human is waiting; tasks are autonomous and can wait.
        if state.pending_messages:
            state.active = True
            state.active_is_task = False
            state.pending_messages = False
            self._active_count += 1
            asyncio.ensure_future(self._run_for_group(group_jid, "drain"))
            return

        # Then pending tasks
        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            state.active = True
            state.active_is_task = True
            self._active_count += 1
            asyncio.ensure_future(self._run_task(group_jid, task))
            return

        # Nothing pending for this group; check if other groups are waiting
        self._drain_waiting()

    def _drain_waiting(self) -> None:
        while self._waiting_groups and self._active_count < MAX_CONCURRENT_CONTAINERS:
            next_jid = self._waiting_groups.pop(0)
            state = self._get_group(next_jid)

            # Messages first — same priority as _drain_group
            if state.pending_messages:
                state.active = True
                state.active_is_task = False
                state.pending_messages = False
                self._active_count += 1
                asyncio.ensure_future(self._run_for_group(next_jid, "drain"))
            elif state.pending_tasks:
                task = state.pending_tasks.pop(0)
                state.active = True
                state.active_is_task = True
                self._active_count += 1
                asyncio.ensure_future(self._run_task(next_jid, task))

    async def shutdown(self, grace_period_seconds: float) -> None:
        self._shutting_down = True

        active: list[tuple[Any, str]] = []
        for _jid, state in self._groups.items():
            proc_alive = getattr(state.process, "returncode", None) is None
            if state.process and state.container_name and proc_alive:
                active.append((state.process, state.container_name))

        if not active:
            logger.info("GroupQueue shutdown, no active containers")
            return

        logger.info(
            "GroupQueue shutting down, stopping containers",
            active_count=len(active),
            containers=[name for _, name in active],
        )

        from pynchy.container_runner import _graceful_stop

        await asyncio.gather(
            *(_graceful_stop(proc, name) for proc, name in active),
            return_exceptions=True,
        )
