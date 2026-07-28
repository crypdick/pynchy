"""Awaitable adapters for the per-thread work queue."""

from __future__ import annotations

import asyncio
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves queue annotations at runtime.
    Awaitable,
    Callable,
)
from typing import cast

from pynchy.host.orchestrator.queue_state import (  # noqa: TC001, RUF100 - beartype resolves queue annotations at runtime.
    GroupState,
    QueuedTask,
)
from pynchy.identifiers import (
    RuntimeId,  # noqa: TC001, RUF100 - beartype resolves queue annotations at runtime.
)
from pynchy.turn_outcomes import (  # noqa: TC001, RUF100 - beartype resolves queue annotations at runtime.
    TurnOutcome,
)
from pynchy.workspace.api import (
    RuntimeTarget,  # noqa: TC001, RUF100 - beartype resolves queue annotations at runtime.
)


async def await_queued_task[ResultT](  # noqa: PLR0913, RUF100 - queue adapters keep individual lifecycle controls explicit.
    enqueue_task: Callable[[RuntimeTarget, QueuedTask], bool],
    cancel_task: Callable[[RuntimeId, QueuedTask], bool],
    stop_active_process: Callable[[RuntimeId], Awaitable[None]],
    target: RuntimeTarget,
    task_id: str,
    fn: Callable[[], Awaitable[ResultT]],
) -> ResultT:
    """Run autonomous work through the runtime queue with linked cancellation."""
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
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            runner = None

    def cancel_waiter() -> None:
        future.cancel()

    task = QueuedTask(id=task_id, fn=queued, cancel_waiter=cancel_waiter)
    if not enqueue_task(target, task):
        raise RuntimeError(f"Thread queue rejected scheduled task {task_id}")
    try:
        return await future
    except asyncio.CancelledError:
        future.cancel()
        cancel_task(target.id, task)
        if runner is not None:
            try:
                await stop_active_process(target.id)
            finally:
                runner.cancel()
        raise


async def await_message_turn(
    get_group: Callable[[RuntimeTarget], GroupState],
    enqueue_message_check: Callable[[RuntimeTarget], None],
    target: RuntimeTarget,
    *,
    shutting_down: bool,
) -> TurnOutcome:
    """Wait until a thread's queued interactive turn has executed."""
    if shutting_down:
        raise RuntimeError("Thread queue is shutting down")
    future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    get_group(target).message_waiters.append(future)
    enqueue_message_check(target)
    return cast("TurnOutcome", await future)
