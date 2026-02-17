"""SQLite event observer plugin."""

from __future__ import annotations

from typing import Any

import pluggy

from .observer import SqliteEventObserver

hookimpl = pluggy.HookimplMarker("pynchy")


class SqliteObserverPlugin:
    """Plugin providing SQLite-backed event persistence."""

    @hookimpl
    def pynchy_observer(self) -> Any | None:
        return SqliteEventObserver()
