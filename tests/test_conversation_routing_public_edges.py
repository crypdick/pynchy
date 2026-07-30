"""Public lifecycle edge tests for durable routed conversation state."""

from __future__ import annotations

import pytest

from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationDeliveryStatus,
    ConversationId,
)
from pynchy.identifiers import GroupFolder, SessionId
from pynchy.state import (
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    list_pending_conversation_ids,
    list_route_conversation_ids,
    rebind_conversation_workspace,
    release_conversation_delivery_claim,
    resolve_conversation,
    set_conversation_session,
)
from tests.conversation_routing_support import _admit, _subject

pytest_plugins = ("tests.conversation_routing_support",)


async def test_route_indexes_and_claim_release_paths_are_publicly_observable() -> None:
    first = await _admit("edge-delivery-1", "edge-issue-1")
    second = await _admit("edge-delivery-2", "edge-issue-2")

    provider = first.delivery.identity.provider
    route = first.delivery.identity.route
    pending = await list_pending_conversation_ids(provider, route)
    routed = await list_route_conversation_ids(provider, route)
    assert set(pending) == {first.conversation.id, second.conversation.id}
    assert set(routed) == {first.conversation.id, second.conversation.id}

    assert await claim_next_conversation_delivery(
        first.conversation.id, ConversationClaimId("edge-claim")
    )
    released = await release_conversation_delivery_claim(ConversationClaimId("edge-claim"))
    assert released is not None
    assert released.status is ConversationDeliveryStatus.PENDING
    assert await complete_conversation_delivery(ConversationClaimId("missing-claim")) is None
    assert await release_conversation_delivery_claim(ConversationClaimId("missing-claim")) is None
    assert (
        await claim_next_conversation_delivery(
            ConversationId("missing-conversation"), ConversationClaimId("missing-claim")
        )
        is None
    )


async def test_workspace_and_session_mutations_report_unknown_conversations() -> None:
    conversation = await resolve_conversation(_subject("edge-rebind"), GroupFolder("triage"))

    unchanged = await rebind_conversation_workspace(conversation.id, GroupFolder("triage"))
    moved = await rebind_conversation_workspace(conversation.id, GroupFolder("engineering"))
    cleared = await set_conversation_session(conversation.id, None)
    assert unchanged.workspace == GroupFolder("triage")
    assert moved.workspace == GroupFolder("engineering")
    assert cleared.session_id is None

    with pytest.raises(ValueError, match="Unknown conversation"):
        await rebind_conversation_workspace(ConversationId("missing"), GroupFolder("triage"))
    with pytest.raises(ValueError, match="Unknown conversation"):
        await set_conversation_session(ConversationId("missing"), SessionId("session"))
