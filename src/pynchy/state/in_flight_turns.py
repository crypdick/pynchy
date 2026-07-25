"""Durable checkpoints for agent work interrupted before finalization."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pynchy.conversation.models import (
    ConversationDeliveryCompletion,
    ConversationId,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any

from pynchy.state.connection import _get_db, atomic_write
from pynchy.types import InFlightTurn, InFlightWorkKind

# NOTE: Update docs/architecture/message-routing.md § Interrupted Turn Recovery
# when this ledger's lifecycle or source-of-truth semantics change.


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_turn(row: Row) -> InFlightTurn:
    messages = json.loads(row["input_messages"])
    if not isinstance(messages, list):
        raise TypeError("in_flight_turns.input_messages must decode to a list")
    return InFlightTurn(
        turn_id=row["turn_id"],
        chat_jid=row["chat_jid"],
        group_folder=row["group_folder"],
        work_kind=InFlightWorkKind(row["work_kind"]),
        input_messages=messages,
        input_start_cursor=row["input_start_cursor"],
        input_end_cursor=row["input_end_cursor"],
        started_at=row["started_at"],
        task_id=row["task_id"],
        session_id=row["session_id"],
        output_sent=bool(row["output_sent"]),
        interrupted_at=row["interrupted_at"],
        deploy_id=row["deploy_id"],
        claimed_at=row["claimed_at"],
        scheduled_base_chat_jid=row["scheduled_base_chat_jid"],
        scheduled_thread_slot=row["scheduled_thread_slot"],
        conversation_claim_id=row["conversation_claim_id"],
        input_source=row["input_source"],
    )


async def begin_in_flight_turn(turn: InFlightTurn) -> None:
    """Persist a turn before its agent invocation starts."""
    db = _get_db()
    await db.execute(
        """
        INSERT INTO in_flight_turns (
            turn_id, chat_jid, group_folder, work_kind, input_messages,
            input_start_cursor, input_end_cursor, started_at, task_id,
            session_id, output_sent, interrupted_at, deploy_id, claimed_at,
            scheduled_base_chat_jid, scheduled_thread_slot,
            conversation_claim_id, input_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            turn.turn_id,
            turn.chat_jid,
            turn.group_folder,
            turn.work_kind.value,
            json.dumps(turn.input_messages),
            turn.input_start_cursor,
            turn.input_end_cursor,
            turn.started_at,
            turn.task_id,
            turn.session_id,
            int(turn.output_sent),
            turn.interrupted_at,
            turn.deploy_id,
            turn.claimed_at,
            turn.scheduled_base_chat_jid,
            turn.scheduled_thread_slot,
            turn.conversation_claim_id,
            turn.input_source,
        ),
    )
    await db.commit()


async def get_in_flight_turn(turn_id: str) -> InFlightTurn | None:
    db = _get_db()
    cursor = await db.execute("SELECT * FROM in_flight_turns WHERE turn_id = ?", (turn_id,))
    row = await cursor.fetchone()
    return _row_to_turn(row) if row else None


async def get_in_flight_turns() -> list[InFlightTurn]:
    db = _get_db()
    cursor = await db.execute("SELECT * FROM in_flight_turns ORDER BY started_at, turn_id")
    return [_row_to_turn(row) for row in await cursor.fetchall()]


async def get_in_flight_turn_for_chat(
    chat_jid: str,
    work_kinds: set[InFlightWorkKind],
) -> InFlightTurn | None:
    """Return the oldest resumable turn for a chat in the requested work kinds."""
    if not work_kinds:
        return None
    db = _get_db()
    placeholders = ",".join("?" for _ in work_kinds)
    cursor = await db.execute(
        f"""
        SELECT * FROM in_flight_turns
        WHERE chat_jid = ? AND work_kind IN ({placeholders})
        ORDER BY started_at, turn_id
        LIMIT 1
        """,  # noqa: S608, RUF100 - placeholders are generated only from the enum set size.
        (chat_jid, *(kind.value for kind in work_kinds)),
    )
    row = await cursor.fetchone()
    return _row_to_turn(row) if row else None


async def get_in_flight_turn_for_task(task_id: str) -> InFlightTurn | None:
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM in_flight_turns
        WHERE task_id = ? AND work_kind = ?
        ORDER BY started_at, turn_id
        LIMIT 1
        """,
        (task_id, InFlightWorkKind.SCHEDULED.value),
    )
    row = await cursor.fetchone()
    return _row_to_turn(row) if row else None


async def get_in_flight_turn_for_group(group_folder: str) -> InFlightTurn | None:
    """Return the active turn for one workspace, including scheduled task provenance."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM in_flight_turns
        WHERE group_folder = ?
        ORDER BY started_at DESC, turn_id DESC
        LIMIT 1
        """,
        (group_folder,),
    )
    row = await cursor.fetchone()
    return _row_to_turn(row) if row else None


async def claim_in_flight_turn(turn_id: str) -> bool:
    """Atomically claim a turn so competing Temporal workflows cannot duplicate it."""
    db = _get_db()
    cursor = await db.execute(
        "UPDATE in_flight_turns SET claimed_at = ? WHERE turn_id = ? AND claimed_at IS NULL",
        (_timestamp(), turn_id),
    )
    await db.commit()
    return cursor.rowcount == 1


async def release_in_flight_turn_claim(turn_id: str) -> None:
    db = _get_db()
    await db.execute("UPDATE in_flight_turns SET claimed_at = NULL WHERE turn_id = ?", (turn_id,))
    await db.commit()


async def mark_in_flight_output_sent(turn_id: str) -> None:
    db = _get_db()
    await db.execute("UPDATE in_flight_turns SET output_sent = 1 WHERE turn_id = ?", (turn_id,))
    await db.commit()


async def update_in_flight_session(group_folder: str, session_id: str) -> None:
    """Attach a newly learned agent thread ID to work currently running for a group."""
    db = _get_db()
    await db.execute(
        "UPDATE in_flight_turns SET session_id = ? WHERE group_folder = ?",
        (session_id, group_folder),
    )
    await db.commit()


async def prepare_in_flight_turn_recovery(deploy_id: str | None) -> list[InFlightTurn]:
    """Mark surviving rows interrupted and release claims left by the stopped process."""
    db = _get_db()
    interrupted_at = _timestamp()
    await db.execute(
        """
        UPDATE in_flight_turns
        SET interrupted_at = COALESCE(interrupted_at, ?),
            deploy_id = COALESCE(?, deploy_id),
            claimed_at = NULL
        """,
        (interrupted_at, deploy_id),
    )
    await db.commit()
    return await get_in_flight_turns()


async def clear_in_flight_turn(turn_id: str) -> None:
    db = _get_db()
    await db.execute("DELETE FROM in_flight_turns WHERE turn_id = ?", (turn_id,))
    await db.commit()


async def clear_unclaimed_in_flight_turn_for_task(task_id: str) -> bool:
    """Clear a terminal scheduled checkpoint unless a recovery worker owns it."""
    db = _get_db()
    cursor = await db.execute(
        """
        DELETE FROM in_flight_turns
        WHERE task_id = ? AND work_kind = ? AND claimed_at IS NULL
        """,
        (task_id, InFlightWorkKind.SCHEDULED.value),
    )
    await db.commit()
    return cursor.rowcount > 0


async def complete_in_flight_turn(
    turn_id: str,
    *,
    last_agent_timestamps: dict[str, str] | None = None,
    conversation_claim_id: str | None = None,
) -> ConversationDeliveryCompletion | None:
    """Finalize a turn atomically with its cursor and routed-delivery claim."""
    async with atomic_write() as db:
        if last_agent_timestamps is not None:
            await db.execute(
                "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
                ("last_agent_timestamp", json.dumps(last_agent_timestamps)),
            )
        await db.execute("DELETE FROM in_flight_turns WHERE turn_id = ?", (turn_id,))
        completed: ConversationDeliveryCompletion | None = None
        if conversation_claim_id is not None:
            claimed_cursor = await db.execute(
                """
                SELECT provider, route, delivery_id, conversation_id
                FROM conversation_deliveries
                WHERE claim_id = ? AND status = 'claimed'
                """,
                (conversation_claim_id,),
            )
            claimed = await claimed_cursor.fetchone()
            if claimed is None:
                raise ValueError("Conversation delivery claim disappeared before turn completion")
            cursor = await db.execute(
                """
                UPDATE conversation_deliveries
                SET status = 'completed', completed_at = ?
                WHERE claim_id = ? AND status = 'claimed'
                """,
                (_timestamp(), conversation_claim_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Conversation delivery claim disappeared before turn completion")
            completed = ConversationDeliveryCompletion(
                identity=ExternalDeliveryIdentity(
                    provider=ExternalProvider(claimed["provider"]),
                    route=ExternalRoute(claimed["route"]),
                    delivery_id=ExternalDeliveryId(claimed["delivery_id"]),
                ),
                conversation_id=ConversationId(claimed["conversation_id"]),
            )
        return completed
