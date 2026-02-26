"""Event storage — persists EventBus events to the ``events`` table."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pynchy.db._connection import _get_db


async def store_event(
    event_type: str,
    chat_jid: str | None,
    payload: dict,
) -> None:
    """Insert an event row into the ``events`` table.

    Best-effort storage for EventBus observers. Callers should catch
    exceptions if they don't want a storage failure to propagate.
    """
    db = _get_db()
    await db.execute(
        "INSERT INTO events (event_type, chat_jid, timestamp, payload) VALUES (?, ?, ?, ?)",
        (event_type, chat_jid, datetime.now(UTC).isoformat(), json.dumps(payload)),
    )
    await db.commit()
