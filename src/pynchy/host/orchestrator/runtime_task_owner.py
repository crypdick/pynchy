"""Ownership and teardown for long-lived subsystem tasks."""

from __future__ import annotations

import asyncio
from typing import Any


class RuntimeTaskOwner:
    """Collect runtime tasks and join their cancellation before rollback."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[Any]] = []

    def add(self, task: asyncio.Task[Any]) -> None:
        self._tasks.append(task)

    async def stop(self) -> None:
        tasks = self._tasks.copy()
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
