"""SQLite event observer plugin."""

from __future__ import annotations

from collections.abc import (  # beartype resolves observer callback annotations at runtime.
    Awaitable,
    Callable,
)

import pluggy

from .observer import SqliteEventObserver

__all__ = ["SqliteObserverPlugin"]

hookimpl = pluggy.HookimplMarker("pynchy")

type StoreEvent = Callable[[str, str | None, dict[str, object]], Awaitable[None]]


class SqliteObserverPlugin:
    """Plugin providing SQLite-backed event persistence."""

    def __init__(self) -> None:
        self._store_event: StoreEvent | None = None

    def configure(
        self,
        *,
        store_event: StoreEvent,
    ) -> None:
        """Set durable event persistence before the observer is attached."""
        self._store_event = store_event

    @hookimpl
    def pynchy_observer(self) -> SqliteEventObserver:
        if self._store_event is None:
            raise RuntimeError("SQLite observer plugin has not been configured")
        return SqliteEventObserver(store_event=self._store_event)
