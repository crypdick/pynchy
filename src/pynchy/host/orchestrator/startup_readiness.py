"""Process-local gate for work that depends on completed startup recovery."""

from __future__ import annotations

import asyncio


class StartupReadinessError(RuntimeError):
    """Startup failed before route-dependent work became safe."""


class StartupReadiness:
    """Release route-dependent work only after startup settles successfully."""

    def __init__(self) -> None:
        self._settled = asyncio.Event()
        self._failure: BaseException | None = None

    async def wait(self) -> None:
        """Wait for startup and raise when recovery did not complete."""
        await self._settled.wait()
        if self._failure is not None:
            raise StartupReadinessError("Startup route recovery failed") from self._failure

    def mark_ready(self) -> None:
        """Release waiters after every route owner has recovered."""
        if not self._settled.is_set():
            self._settled.set()

    def mark_failed(self, failure: BaseException) -> None:
        """Release waiters with the startup failure."""
        if not self._settled.is_set():
            self._failure = failure
            self._settled.set()
