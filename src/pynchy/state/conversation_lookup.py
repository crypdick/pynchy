"""Provider-key lookup for routed conversations."""

from __future__ import annotations

from pynchy.conversation.api import (
    Conversation,  # beartype resolves lookup annotations at runtime.
    ConversationId,
    ConversationSubjectKey,  # beartype resolves lookup annotations at runtime.
)
from pynchy.identifiers import GroupFolder
from pynchy.state.connection import _get_db
from pynchy.state.conversation_routing import get_conversation


async def get_conversation_for_subject_key(
    subject_key: ConversationSubjectKey,
    *,
    workspace: GroupFolder,
    namespace_suffix: str,
) -> Conversation | None:
    """Resolve a provider subject when its tenant namespace is not locally available."""
    cursor = await _get_db().execute(
        """
        SELECT id FROM routed_conversations
        WHERE subject_key = ? AND workspace = ? AND subject_namespace LIKE ?
        ORDER BY created_at DESC
        LIMIT 2
        """,
        (subject_key, workspace, f"%{namespace_suffix}"),
    )
    rows = list(await cursor.fetchall())
    if len(rows) > 1:
        raise RuntimeError("Provider subject key resolves to multiple routed conversations")
    return await get_conversation(ConversationId(rows[0]["id"])) if rows else None
