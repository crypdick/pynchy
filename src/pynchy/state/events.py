"""Event storage — persists EventBus events to the ``events`` table."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pynchy.state.connection import atomic_write


async def store_event(
    event_type: str,
    chat_jid: str | None,
    payload: dict[str, Any],
) -> None:
    """Insert an event row into the ``events`` table.

    Best-effort storage for EventBus observers. Callers should catch
    exceptions if they don't want a storage failure to propagate.
    """
    async with atomic_write() as db:
        await db.execute(
            "INSERT INTO events (event_type, chat_jid, timestamp, payload) VALUES (?, ?, ?, ?)",
            (event_type, chat_jid, datetime.now(UTC).isoformat(), json.dumps(payload)),
        )
