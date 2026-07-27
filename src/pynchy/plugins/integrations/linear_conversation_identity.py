"""Canonical routed identity for Linear issue conversations."""

from __future__ import annotations

from pynchy.conversation.models import (
    Conversation,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.state import (
    get_conversation_for_subject_key,
    get_unfinished_work_item_execution,
    resolve_conversation,
)
from pynchy.types import GroupFolder


async def resolve_linear_issue_conversation(
    issue_id: str,
    workspace: str,
    account_name: str,
) -> Conversation:
    """Return one issue runtime, pinned to any unfinished execution's workspace."""
    execution = await get_unfinished_work_item_execution(issue_id)
    owner_workspace = execution.workspace if execution is not None else workspace
    group_folder = GroupFolder(owner_workspace)
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
