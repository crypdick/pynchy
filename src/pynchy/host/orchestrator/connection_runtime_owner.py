"""Owned lifecycle state for pluggable connection runtimes."""

from __future__ import annotations

from contextlib import AsyncExitStack

from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves runtime annotations.
    ConnectionRuntime,
)


class ConnectionRuntimeOwner:
    """Retain provider runtimes for readiness and graceful shutdown."""

    def __init__(self) -> None:
        self._runtimes: list[ConnectionRuntime] = []

    def set(self, runtimes: list[ConnectionRuntime]) -> None:
        self._runtimes = runtimes

    def runtimes(self) -> tuple[ConnectionRuntime, ...]:
        return tuple(self._runtimes)

    async def close(self) -> None:
        runtimes, self._runtimes = self._runtimes, []
        async with AsyncExitStack() as cleanup:
            for runtime in runtimes:
                cleanup.push_async_callback(runtime.close)

    def status(self) -> dict[str, bool]:
        return {runtime.name: runtime.is_ready() for runtime in self._runtimes}
