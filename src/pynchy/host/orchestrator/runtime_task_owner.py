"""Ownership and teardown for long-lived subsystem tasks."""

from __future__ import annotations

import asyncio
from typing import Any, cast


class RuntimeTaskOwner:
    """Collect runtime tasks and join their cancellation before rollback."""

    def __init__(self) -> None:
        self._tasks: list[object] = []

    def add(self, task: object) -> None:
        self._tasks.append(task)

    async def stop(self) -> None:
        tasks = self._tasks.copy()
        self._tasks.clear()
        for task in tasks:
            cast("Any", task).cancel()
        await asyncio.gather(
            *(cast("Any", task) for task in tasks if isinstance(task, asyncio.Future)),
            return_exceptions=True,
        )
