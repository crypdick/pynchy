"""Canonical routed identity for Linear issue conversations."""

from __future__ import annotations

from pynchy.conversation.models import (
    Conversation,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.state import get_conversation_for_subject_key, resolve_conversation
from pynchy.types import GroupFolder


async def resolve_linear_issue_conversation(
    issue_id: str,
    workspace: str,
    account_name: str,
) -> Conversation:
    """Return or create the issue's sole routed runtime identity."""
    group_folder = GroupFolder(workspace)
    existing = await get_conversation_for_subject_key(
        ConversationSubjectKey(issue_id),
        workspace=group_folder,
        namespace_suffix=":issue",
    )
    if existing is not None:
        return existing
    return await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace(f"linear:{account_name}:issue"),
            key=ConversationSubjectKey(issue_id),
        ),
        group_folder,
    )
