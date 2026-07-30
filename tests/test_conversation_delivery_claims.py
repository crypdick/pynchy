"""FIFO claim behavior for provider-routed conversation deliveries."""

from __future__ import annotations

import pytest

from pynchy.conversation.api import ConversationClaimId
from pynchy.state import (
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    release_conversation_delivery_claim,
)
from tests.conversation_routing_support import _admit

pytest_plugins = ("tests.conversation_routing_support",)


@pytest.mark.asyncio
async def test_delivery_claims_are_fifo_and_require_the_active_token() -> None:
    admission = await _admit("delivery-claims", "issue-claims")
    conversation_id = admission.conversation.id
    first_claim = ConversationClaimId("claim-first")
    second_claim = ConversationClaimId("claim-second")

    claimed = await claim_next_conversation_delivery(conversation_id, first_claim)
    assert claimed is not None
    assert claimed.claim_id == first_claim
    assert await claim_next_conversation_delivery(conversation_id, second_claim) is None
    assert await complete_conversation_delivery(second_claim) is None
    assert await release_conversation_delivery_claim(second_claim) is None

    completed = await complete_conversation_delivery(first_claim)
    assert completed is not None
    assert completed.status.value == "completed"
    assert await claim_next_conversation_delivery(conversation_id, second_claim) is None
