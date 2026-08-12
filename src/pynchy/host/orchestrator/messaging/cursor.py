"""Cursor persistence helpers for message-processing boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pynchy.conversation.api import (
    ConversationDeliveryCompletion,  # beartype resolves completion results.
    notify_conversation_delivery_completed,
)
from pynchy.state.api import complete_in_flight_turn


@runtime_checkable
class CursorDeps(Protocol):
    """Minimal dependencies for cursor persistence."""

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    async def save_state(self) -> None: ...


def _sequence(cursor: str) -> int | None:
    if not cursor.startswith("sequence:"):
        return None
    value = cursor.removeprefix("sequence:")
    return int(value) if value.isdecimal() else None


def monotonic_cursor(previous: str, current: str) -> str:
    """Return the later provider-time or local-sequence cursor."""
    if not previous or not current:
        return current or previous
    previous_sequence = _sequence(previous)
    current_sequence = _sequence(current)
    if previous_sequence is not None or current_sequence is not None:
        if previous_sequence is not None and current_sequence is not None:
            return previous if previous_sequence > current_sequence else current
        return current if current_sequence is not None else previous
    try:
        previous_time = datetime.fromisoformat(previous)
        current_time = datetime.fromisoformat(current)
    except (TypeError, ValueError):
        # Synthetic/test cursors and provider-specific opaque cursors have no
        # meaningful ordering; the completed turn is authoritative for them.
        return current
    else:
        return previous if previous_time > current_time else current


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
    except Exception:  # cursor save is a state boundary; roll back the optimistic advance.
        deps.last_agent_timestamp[chat_jid] = previous_cursor
        raise


async def complete_turn_with_cursor(
    deps: CursorDeps,
    chat_jid: str,
    new_cursor: str,
    turn_id: str,
    *,
    conversation_claim_id: str | None = None,
) -> ConversationDeliveryCompletion | None:
    """Atomically persist a monotonic cursor, turn, and optional routed delivery."""
    previous_cursor = deps.last_agent_timestamp.get(chat_jid, "")
    deps.last_agent_timestamp[chat_jid] = monotonic_cursor(previous_cursor, new_cursor)
    try:
        completed = await complete_in_flight_turn(
            turn_id,
            last_agent_timestamps=deps.last_agent_timestamp,
            conversation_claim_id=conversation_claim_id,
        )
    except Exception:  # cursor persistence rolls back in-memory state.
        deps.last_agent_timestamp[chat_jid] = previous_cursor
        raise
    if completed is not None:
        await notify_conversation_delivery_completed(completed)
    return completed
