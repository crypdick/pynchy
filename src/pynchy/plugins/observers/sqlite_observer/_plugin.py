"""SQLite event observer plugin."""

from __future__ import annotations

import pluggy

from .observer import SqliteEventObserver

__all__ = ["SqliteObserverPlugin"]

hookimpl = pluggy.HookimplMarker("pynchy")


class SqliteObserverPlugin:
    """Plugin providing SQLite-backed event persistence."""

    @hookimpl
    def pynchy_observer(self) -> SqliteEventObserver:
        return SqliteEventObserver()
