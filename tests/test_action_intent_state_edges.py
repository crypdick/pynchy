"""Boundary coverage for durable action-intent state transitions."""

from __future__ import annotations

import pytest

from pynchy.action_intents import ActionIntentStatus
from pynchy.state import (
    ActionIntentCreateRequest,
    approve_action_intent,
    create_action_intent,
    deny_action_intent,
    init_test_database,
    list_action_intents,
)


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _request(request_id: str) -> ActionIntentCreateRequest:
    return ActionIntentCreateRequest(
        request_id=request_id,
        workspace="workspace",
        action_id="action.send",
        tool_name="send_message",
        provider="provider",
        actor_jid="actor@g.us",
        recipient="recipient",
        payload={"body": "hello"},
        source_refs=(),
        summary="Send a message",
    )


async def test_list_action_intents_returns_records_without_workspace_filter() -> None:
    await create_action_intent(_request("request-list"))

    intents = await list_action_intents()

    assert [intent.request_id for intent in intents] == ["request-list"]


async def test_approving_missing_action_intent_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="Action intent not found"):
        await approve_action_intent(
            "request-missing",
            approver="admin",
            approved_at="2026-07-29T00:00:00+00:00",
            policy_decision="approved",
        )


async def test_approving_drafted_action_intent_rejects_wrong_state() -> None:
    await create_action_intent(_request("request-drafted"))
    await deny_action_intent("request-drafted", reason="operator denied")

    with pytest.raises(RuntimeError, match="cannot move from denied"):
        await approve_action_intent(
            "request-drafted",
            approver="admin",
            approved_at="2026-07-29T00:00:00+00:00",
            policy_decision="approved",
        )

    intents = await list_action_intents(workspace="workspace")
    assert intents[0].status is ActionIntentStatus.DENIED
