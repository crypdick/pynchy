"""Cursor persistence helpers for message-processing boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CursorDeps(Protocol):
    """Minimal dependencies for cursor persistence."""

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    async def save_state(self) -> None: ...


async def advance_cursor(
    deps: CursorDeps,
    chat_jid: str,
    new_cursor: str,
) -> None:
    """Persist the processed-message cursor, rolling back on save failure."""
    previous_cursor = deps.last_agent_timestamp.get(chat_jid, "")
    deps.last_agent_timestamp[chat_jid] = new_cursor
    try:
        await deps.save_state()
    except Exception:  # noqa: BLE001, RUF100 - cursor save is a state boundary; roll back the optimistic advance.
        deps.last_agent_timestamp[chat_jid] = previous_cursor
        raise
