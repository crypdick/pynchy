"""Boundary coverage for durable action-intent state transitions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from pynchy.action_intents import ActionIntentStatus
from pynchy.state import (
    ActionIntentCreateRequest,
    approve_action_intent,
    create_action_intent,
    deny_action_intent,
    init_test_database,
    list_action_intents,
    mark_action_intent_awaiting_approval,
    reconcile_action_intent,
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


async def test_duplicate_action_intent_request_reuses_existing_draft() -> None:
    first, created = await create_action_intent(_request("request-duplicate"))
    second, duplicate_created = await create_action_intent(_request("request-duplicate"))

    assert created is True
    assert duplicate_created is False
    assert second.id == first.id


@pytest.mark.parametrize("existing", [True, False])
async def test_insert_race_handles_integrity_error(
    existing: bool,
) -> None:
    first, _ = await create_action_intent(_request("request-race"))
    query = AsyncMock(return_value=first if existing else None)

    with patch("pynchy.state.action_intents.get_action_intent_by_request", new=query):
        if existing:
            result, created = await create_action_intent(_request("request-race"))
            assert result.id == first.id
            assert created is False
        else:
            with pytest.raises(aiosqlite.IntegrityError):
                await create_action_intent(_request("request-race"))


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


async def test_status_update_fails_if_intent_disappears_after_commit() -> None:
    await create_action_intent(_request("request-disappears"))

    with (
        patch(
            "pynchy.state.action_intents.get_action_intent_by_request",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="Action intent disappeared"),
    ):
        await mark_action_intent_awaiting_approval(
            "request-disappears",
            policy_decision="approved",
        )


async def test_reconcile_rejects_a_conflicting_concurrent_receipt() -> None:
    await create_action_intent(_request("request-reconcile-conflict"))

    with (
        patch(
            "pynchy.state.action_intents._set_action_intent_status",
            new=AsyncMock(side_effect=RuntimeError("state changed")),
        ),
        patch(
            "pynchy.state.action_intents.get_action_intent_by_request",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="state changed"),
    ):
        await reconcile_action_intent(
            "request-reconcile-conflict",
            provider_request_id="provider-1",
            receipt={"event_id": "provider-1"},
        )
