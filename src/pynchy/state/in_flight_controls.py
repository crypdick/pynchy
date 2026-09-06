"""Durable pause and reset transitions for in-flight agent turns."""

from __future__ import annotations

import json
from typing import Any

from aiosqlite import (
    Connection,
)

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
)
from pynchy.state.connection import atomic_write
from pynchy.state.in_flight_turns import (
    _get_in_flight_turn_in_transaction,
    _timestamp,
)


async def request_in_flight_turn_control(
    chat_jid: str,
    requested_state: CheckpointControlState,
) -> InFlightTurn | None:
    """Atomically request pause or reset for the oldest checkpoint in one chat."""
    async with atomic_write() as db:
        return await _request_in_flight_turn_control_in_transaction(
            db,
            chat_jid,
            requested_state,
        )


async def consume_in_flight_control_message(
    message_id: str,
    chat_jid: str,
    timestamp: str,
    last_agent_timestamps: dict[str, str],
    requested_state: CheckpointControlState,
) -> InFlightTurn | None:
    """Consume a pause/reset command and checkpoint its transition atomically."""
    async with atomic_write() as db:
        metadata_cursor = await db.execute(
            "SELECT metadata FROM messages WHERE id = ? AND chat_jid = ?",
            (message_id, chat_jid),
        )
        row = await metadata_cursor.fetchone()
        if row is None:
            raise ValueError("Control message disappeared before it could be consumed")
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        if not isinstance(metadata, dict):
            raise TypeError("Control message metadata has an invalid persisted shape")
        metadata.pop("deferred_host_control", None)
        updated = await db.execute(
            """
            UPDATE messages
            SET message_type = 'host', metadata = ?
            WHERE id = ? AND chat_jid = ?
            """,
            (json.dumps(metadata), message_id, chat_jid),
        )
        if updated.rowcount != 1:
            raise ValueError("Control message disappeared before it could be consumed")

        turn = await _request_in_flight_turn_control_in_transaction(
            db,
            chat_jid,
            requested_state,
        )
        cursors = dict(last_agent_timestamps)
        cursors[chat_jid] = max(cursors.get(chat_jid, ""), timestamp)
        await db.execute(
            "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
            ("last_agent_timestamp", json.dumps(cursors)),
        )
        return turn


async def _request_in_flight_turn_control_in_transaction(
    db: Connection,
    chat_jid: str,
    requested_state: CheckpointControlState,
) -> InFlightTurn | None:
    if requested_state not in {
        CheckpointControlState.PAUSE_REQUESTED,
        CheckpointControlState.RESET_REQUESTED,
    }:
        raise ValueError(f"Unsupported requested checkpoint state: {requested_state}")

    cursor = await db.execute(
        """
        SELECT turn_id, control_state
        FROM in_flight_turns
        WHERE chat_jid = ?
        ORDER BY started_at, turn_id
        LIMIT 1
        """,
        (chat_jid,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    current = CheckpointControlState(row["control_state"])
    target: CheckpointControlState
    if requested_state is CheckpointControlState.PAUSE_REQUESTED:
        if current is CheckpointControlState.RESET_REQUESTED:
            return await _get_in_flight_turn_in_transaction(db, row["turn_id"])
        target = (
            CheckpointControlState.PAUSE_REQUESTED
            if current is CheckpointControlState.ACTIVE
            else current
        )
    else:
        target = CheckpointControlState.RESET_REQUESTED
    await db.execute(
        "UPDATE in_flight_turns SET control_state = ? WHERE turn_id = ?",
        (target.value, row["turn_id"]),
    )
    return await _get_in_flight_turn_in_transaction(db, row["turn_id"])


async def finalize_in_flight_pause(turn_id: str) -> bool:
    """Finish a pause request and release its execution claim."""
    async with atomic_write() as db:
        cursor = await db.execute(
            """
            UPDATE in_flight_turns
            SET control_state = ?, claimed_at = NULL
            WHERE turn_id = ? AND control_state IN (?, ?)
            """,
            (
                CheckpointControlState.PAUSED.value,
                turn_id,
                CheckpointControlState.PAUSE_REQUESTED.value,
                CheckpointControlState.PAUSED.value,
            ),
        )
    return cursor.rowcount == 1


async def resume_paused_in_flight_turn(
    turn_id: str,
    guidance_messages: list[dict[str, Any]],
    input_end_cursor: str,
    *,
    claim: bool,
) -> InFlightTurn | None:
    """Restore a paused checkpoint and durably attach user guidance."""
    async with atomic_write() as db:
        turn = await _get_in_flight_turn_in_transaction(db, turn_id)
        if turn is None or turn.control_state is not CheckpointControlState.PAUSED:
            return None
        messages = [
            *turn.input_messages,
            *[
                {
                    **message,
                    "metadata": {
                        **(message.get("metadata") or {}),
                        "checkpoint_guidance": True,
                    },
                }
                for message in guidance_messages
            ],
        ]
        claimed_at = _timestamp() if claim else None
        cursor = await db.execute(
            """
            UPDATE in_flight_turns
            SET input_messages = ?, input_end_cursor = ?,
                control_state = ?, claimed_at = ?
            WHERE turn_id = ? AND control_state = ? AND claimed_at IS NULL
            """,
            (
                json.dumps(messages),
                input_end_cursor,
                CheckpointControlState.ACTIVE.value,
                claimed_at,
                turn_id,
                CheckpointControlState.PAUSED.value,
            ),
        )
        if cursor.rowcount != 1:
            return None
        return await _get_in_flight_turn_in_transaction(db, turn_id)
