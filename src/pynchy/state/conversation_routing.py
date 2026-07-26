"""Durable identity, delivery, and control-binding state for routed conversations."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pynchy.conversation.models import (
    Conversation,
    ConversationClaimId,
    ConversationDelivery,
    ConversationDeliveryAdmission,
    ConversationDeliveryStatus,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.webhook_effect_decisions import webhook_effect_delivery_status
from pynchy.types import GroupFolder, SessionId

if TYPE_CHECKING:
    from aiosqlite import Connection, Row
else:
    Connection = Any
    Row = Any

# NOTE: Update docs/architecture/conversation-routing.md when changing these
# identity or delivery-claim source-of-truth semantics.


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _new_conversation_id() -> ConversationId:
    return ConversationId(f"conv_{secrets.token_urlsafe(18)}")


def _row_to_conversation(row: Row) -> Conversation:
    return Conversation(
        id=ConversationId(row["id"]),
        workspace=GroupFolder(row["workspace"]),
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace(row["subject_namespace"]),
            key=ConversationSubjectKey(row["subject_key"]),
        ),
        session_id=SessionId(row["session_id"]) if row["session_id"] is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_delivery(row: Row) -> ConversationDelivery:
    raw_payload = row["payload"]
    payload = json.loads(raw_payload) if raw_payload is not None else None
    if payload is not None and not isinstance(payload, dict):
        raise TypeError("Conversation delivery payload has an invalid persisted shape")
    return ConversationDelivery(
        sequence=row["sequence"],
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider(row["provider"]),
            route=ExternalRoute(row["route"]),
            delivery_id=ExternalDeliveryId(row["delivery_id"]),
        ),
        conversation_id=ConversationId(row["conversation_id"]),
        status=ConversationDeliveryStatus(row["status"]),
        received_at=row["received_at"],
        payload=payload,
        claim_id=(ConversationClaimId(row["claim_id"]) if row["claim_id"] is not None else None),
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
    )


async def _conversation_for_subject(
    database: Connection,
    subject: ConversationSubject,
) -> Conversation | None:
    cursor = await database.execute(
        """
        SELECT * FROM routed_conversations
        WHERE subject_namespace = ? AND subject_key = ?
        """,
        (subject.namespace, subject.key),
    )
    row = await cursor.fetchone()
    return _row_to_conversation(row) if row is not None else None


async def _conversation_by_id(
    database: Connection,
    conversation_id: ConversationId,
) -> Conversation | None:
    cursor = await database.execute(
        "SELECT * FROM routed_conversations WHERE id = ?",
        (conversation_id,),
    )
    row = await cursor.fetchone()
    return _row_to_conversation(row) if row is not None else None


async def _resolve_conversation(
    database: Connection,
    subject: ConversationSubject,
    workspace: GroupFolder,
) -> Conversation:
    existing = await _conversation_for_subject(database, subject)
    if existing is not None:
        if existing.workspace == workspace:
            return existing
        now = _timestamp()
        await database.execute(
            "UPDATE routed_conversations SET workspace = ?, updated_at = ? WHERE id = ?",
            (workspace, now, existing.id),
        )
        moved = await _conversation_by_id(database, existing.id)
        if moved is None:
            raise RuntimeError("Conversation disappeared while updating its workspace")
        return moved

    now = _timestamp()
    conversation = Conversation(
        id=_new_conversation_id(),
        workspace=workspace,
        subject=subject,
        session_id=None,
        created_at=now,
        updated_at=now,
    )
    await database.execute(
        """
        INSERT INTO routed_conversations (
            id, workspace, subject_namespace, subject_key, session_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conversation.id,
            conversation.workspace,
            conversation.subject.namespace,
            conversation.subject.key,
            conversation.session_id,
            conversation.created_at,
            conversation.updated_at,
        ),
    )
    return conversation


async def resolve_conversation(
    subject: ConversationSubject,
    workspace: GroupFolder,
) -> Conversation:
    """Resolve one immutable subject and update its mutable workspace placement."""
    async with atomic_write() as database:
        return await _resolve_conversation(database, subject, workspace)


async def get_conversation(conversation_id: ConversationId) -> Conversation | None:
    """Return one durable conversation by its opaque identity."""
    return await _conversation_by_id(_get_db(), conversation_id)


async def get_conversation_for_subject(
    subject: ConversationSubject,
) -> Conversation | None:
    """Return the conversation for an immutable external subject."""
    return await _conversation_for_subject(_get_db(), subject)


async def rebind_conversation_workspace(
    conversation_id: ConversationId,
    workspace: GroupFolder,
) -> Conversation:
    """Move conversation placement without changing identity or agent context."""
    async with atomic_write() as database:
        conversation = await _conversation_by_id(database, conversation_id)
        if conversation is None:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        if conversation.workspace != workspace:
            await database.execute(
                "UPDATE routed_conversations SET workspace = ?, updated_at = ? WHERE id = ?",
                (workspace, _timestamp(), conversation_id),
            )
        rebound = await _conversation_by_id(database, conversation_id)
        if rebound is None:
            raise RuntimeError("Conversation disappeared while rebinding its workspace")
        return rebound


async def set_conversation_session(
    conversation_id: ConversationId,
    session_id: SessionId | None,
) -> Conversation:
    """Attach agent context to conversation identity, independent of its control thread."""
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            UPDATE routed_conversations SET session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (session_id, _timestamp(), conversation_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        conversation = await _conversation_by_id(database, conversation_id)
        if conversation is None:
            raise RuntimeError("Conversation disappeared while updating its session")
        return conversation


async def _delivery_by_identity(
    database: Connection,
    identity: ExternalDeliveryIdentity,
) -> ConversationDelivery | None:
    cursor = await database.execute(
        """
        SELECT * FROM conversation_deliveries
        WHERE provider = ? AND route = ? AND delivery_id = ?
        """,
        (identity.provider, identity.route, identity.delivery_id),
    )
    row = await cursor.fetchone()
    return _row_to_delivery(row) if row is not None else None


async def _admit_conversation_delivery(
    database: Connection,
    identity: ExternalDeliveryIdentity,
    subject: ConversationSubject,
    workspace: GroupFolder,
    *,
    payload: dict[str, Any] | None = None,
) -> ConversationDeliveryAdmission | None:
    existing_delivery = await _delivery_by_identity(database, identity)
    if existing_delivery is not None:
        conversation = await _conversation_by_id(database, existing_delivery.conversation_id)
        if conversation is None:
            raise RuntimeError("Conversation delivery references a missing conversation")
        if conversation.subject != subject:
            raise ValueError("External delivery is already linked to another subject")
        return ConversationDeliveryAdmission(
            conversation=conversation,
            delivery=existing_delivery,
            created=False,
        )

    receipt_cursor = await database.execute(
        """
        SELECT received_at FROM external_receipts
        WHERE provider = ? AND route = ? AND delivery_id = ?
        """,
        (identity.provider, identity.route, identity.delivery_id),
    )
    receipt = await receipt_cursor.fetchone()
    if receipt is None:
        raise ValueError("External delivery requires an authenticated receipt")

    status = await webhook_effect_delivery_status(database, identity)
    if status is None:
        return None
    conversation = await _resolve_conversation(database, subject, workspace)
    cursor = await database.execute(
        """
        INSERT INTO conversation_deliveries (
            provider, route, delivery_id, conversation_id, status, received_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identity.provider,
            identity.route,
            identity.delivery_id,
            conversation.id,
            status.value,
            receipt["received_at"],
            json.dumps(payload, sort_keys=True) if payload is not None else None,
        ),
    )
    sequence = cursor.lastrowid
    if sequence is None:
        raise RuntimeError("Conversation delivery insert returned no sequence")
    delivery = ConversationDelivery(
        sequence=sequence,
        identity=identity,
        conversation_id=conversation.id,
        status=status,
        received_at=receipt["received_at"],
        payload=payload,
    )
    return ConversationDeliveryAdmission(
        conversation=conversation,
        delivery=delivery,
        created=True,
    )


async def get_conversation_delivery(
    identity: ExternalDeliveryIdentity,
) -> ConversationDelivery | None:
    """Return the FIFO entry linked to one external delivery."""
    return await _delivery_by_identity(_get_db(), identity)


async def list_pending_conversation_ids(
    provider: ExternalProvider,
    route: ExternalRoute,
) -> tuple[ConversationId, ...]:
    """Return conversations with queued work for one exact provider route."""
    cursor = await _get_db().execute(
        """
        SELECT DISTINCT conversation_id FROM conversation_deliveries
        WHERE provider = ? AND route = ? AND status = 'pending'
        ORDER BY conversation_id
        """,
        (provider, route),
    )
    return tuple(ConversationId(row["conversation_id"]) for row in await cursor.fetchall())


async def list_route_conversation_ids(
    provider: ExternalProvider,
    route: ExternalRoute,
) -> tuple[ConversationId, ...]:
    """Return every durable conversation admitted through one exact route."""
    cursor = await _get_db().execute(
        """
        SELECT DISTINCT conversation_id FROM conversation_deliveries
        WHERE provider = ? AND route = ?
        ORDER BY conversation_id
        """,
        (provider, route),
    )
    return tuple(ConversationId(row["conversation_id"]) for row in await cursor.fetchall())


async def claim_next_conversation_delivery(
    conversation_id: ConversationId,
    claim_id: ConversationClaimId,
) -> ConversationDelivery | None:
    """Claim the oldest pending delivery when no sibling delivery is running."""
    async with atomic_write() as database:
        claimed_cursor = await database.execute(
            """
            SELECT 1 FROM conversation_deliveries
            WHERE conversation_id = ? AND status = 'claimed'
            LIMIT 1
            """,
            (conversation_id,),
        )
        if await claimed_cursor.fetchone() is not None:
            return None

        head_cursor = await database.execute(
            """
            SELECT sequence, status FROM conversation_deliveries
            WHERE conversation_id = ? AND status != 'completed'
            ORDER BY sequence
            LIMIT 1
            """,
            (conversation_id,),
        )
        head = await head_cursor.fetchone()
        if head is None or head["status"] != ConversationDeliveryStatus.PENDING.value:
            return None

        claimed_at = _timestamp()
        await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'claimed', claim_id = ?, claimed_at = ?
            WHERE sequence = ? AND status = 'pending'
            """,
            (claim_id, claimed_at, head["sequence"]),
        )
        cursor = await database.execute(
            "SELECT * FROM conversation_deliveries WHERE sequence = ?",
            (head["sequence"],),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Conversation delivery disappeared while claiming it")
        return _row_to_delivery(row)


async def complete_conversation_delivery(
    claim_id: ConversationClaimId,
) -> ConversationDelivery | None:
    """Complete the delivery held by a claim token."""
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            SELECT sequence FROM conversation_deliveries
            WHERE claim_id = ? AND status = 'claimed'
            """,
            (claim_id,),
        )
        claimed = await cursor.fetchone()
        if claimed is None:
            return None
        await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'completed', completed_at = ?
            WHERE sequence = ?
            """,
            (_timestamp(), claimed["sequence"]),
        )
        result_cursor = await database.execute(
            "SELECT * FROM conversation_deliveries WHERE sequence = ?",
            (claimed["sequence"],),
        )
        row = await result_cursor.fetchone()
        if row is None:
            raise RuntimeError("Conversation delivery disappeared while completing it")
        return _row_to_delivery(row)


async def release_conversation_delivery_claim(
    claim_id: ConversationClaimId,
) -> ConversationDelivery | None:
    """Return one claimed delivery to its FIFO position for a safe retry."""
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            SELECT sequence FROM conversation_deliveries
            WHERE claim_id = ? AND status = 'claimed'
            """,
            (claim_id,),
        )
        claimed = await cursor.fetchone()
        if claimed is None:
            return None
        await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'pending', claim_id = NULL, claimed_at = NULL
            WHERE sequence = ?
            """,
            (claimed["sequence"],),
        )
        result_cursor = await database.execute(
            "SELECT * FROM conversation_deliveries WHERE sequence = ?",
            (claimed["sequence"],),
        )
        row = await result_cursor.fetchone()
        if row is None:
            raise RuntimeError("Conversation delivery disappeared while releasing its claim")
        return _row_to_delivery(row)
