"""Behavioral coverage for provider-key conversation lookup."""

from __future__ import annotations

import pytest

from pynchy.conversation.api import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.identifiers import GroupFolder
from pynchy.state import (
    get_conversation_for_subject_key,
    init_test_database,
    resolve_conversation,
)


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


async def test_ambiguous_provider_subject_lookup_is_rejected() -> None:
    """A tenant-independent lookup never picks an arbitrary issue runtime."""
    workspace = GroupFolder("project")
    issue_key = ConversationSubjectKey("issue-1")
    for tenant in ("alpha", "beta"):
        await resolve_conversation(
            ConversationSubject(
                namespace=ConversationSubjectNamespace(f"linear:{tenant}:issue"),
                key=issue_key,
            ),
            workspace,
        )

    with pytest.raises(RuntimeError, match="multiple routed conversations"):
        await get_conversation_for_subject_key(
            issue_key,
            workspace=workspace,
            namespace_suffix=":issue",
        )
