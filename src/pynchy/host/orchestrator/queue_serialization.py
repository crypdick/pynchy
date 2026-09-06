"""Awaitable adapters for the per-thread work queue."""

from __future__ import annotations

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
)

from pynchy.host.orchestrator.queue_state import (
    # beartype resolves queue annotations at runtime.
    QueuedTask,
)
from pynchy.identifiers import (
    RuntimeId,
)
from pynchy.workspace.api import (
    RuntimeTarget,
)


async def await_queued_task[ResultT](  # noqa: PLR0913 - queue adapters keep individual lifecycle controls explicit.
    enqueue_task: Callable[[RuntimeTarget, QueuedTask], bool],
    cancel_task: Callable[[RuntimeId, QueuedTask], bool],
    stop_active_process: Callable[[RuntimeId], Awaitable[None]],
    target: RuntimeTarget,
    task_id: str,
    fn: Callable[[], Awaitable[ResultT]],
    *,
    is_interactive: bool = False,
) -> ResultT:
    """Run one owned operation through the queue with linked cancellation."""
    future: asyncio.Future[ResultT] = asyncio.get_running_loop().create_future()
    runner: asyncio.Task[None] | None = None

    async def queued() -> None:
        nonlocal runner
        if future.cancelled():
            return
        runner = asyncio.current_task()
        try:
            result = await fn()
            if not future.done():
                future.set_result(result)
        except BaseException as exc:  # noqa: BLE001 - allow: exception-handling; relay failures and cancellation to the awaiting owner.
            if not future.done():
                future.set_exception(exc)
        finally:
            runner = None

    def cancel_waiter() -> None:
        future.cancel()

    task = QueuedTask(id=task_id, fn=queued, cancel=cancel_waiter, is_interactive=is_interactive)
    if not enqueue_task(target, task):
        raise RuntimeError(f"Thread queue rejected scheduled task {task_id}")
    try:
        return await future
    except asyncio.CancelledError:
        future.cancel()
        cancel_task(target.id, task)
        cancelled_runner = runner
        if cancelled_runner is not None:
            try:
                await stop_active_process(target.id)
            finally:
                cancelled_runner.cancel()
        raise
