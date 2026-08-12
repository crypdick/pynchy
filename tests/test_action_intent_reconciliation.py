"""Public behavior tests for external-action reconciliation."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from pynchy.action_intents import ActionIntentStatus
from pynchy.host.orchestrator.api import execute_action_intent, prepare_action_intent
from pynchy.plugins.api import ActionIntentReceipt
from pynchy.state import reconcile_action_intent
from tests.action_intents_support import _prepared_approved_intent, _transactional_action

pytest_plugins = ("tests.action_intents_support",)


@pytest.mark.asyncio
async def test_unknown_action_replay_reconciles_without_a_second_provider_write():
    handler = AsyncMock(return_value={"error": "provider response was lost"})
    action = _transactional_action(handler)
    assert action.action_intent is not None
    reconciler = AsyncMock(
        return_value=ActionIntentReceipt(
            provider_request_id="$reconciled",
            receipt={"event_id": "$reconciled", "room_id": "!room:test"},
        )
    )
    action = replace(
        action,
        action_intent=replace(
            action.action_intent,
            execution_data_from_request=lambda data, request_id: {
                **data,
                "internal_request_id": request_id,
            },
            reconcile_unknown=reconciler,
        ),
    )
    request_id = "request-reconcile"
    await _prepared_approved_intent(action, request_id)

    first = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id=request_id,
    )
    replayed, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )

    assert first == {"error": "External action outcome is unknown; do not retry automatically."}
    handler.assert_awaited_once_with(
        {"room_id": "!room:test", "body": "private payload", "internal_request_id": request_id}
    )
    reconciler.assert_awaited_once()
    assert replayed is not None
    assert replayed.status is ActionIntentStatus.CONFIRMED
    assert replayed.error is None
    assert replay == {
        "result": json.dumps({"event_id": "$reconciled", "room_id": "!room:test"}, sort_keys=True)
    }
    repeated = await reconcile_action_intent(
        request_id,
        provider_request_id="$reconciled",
        receipt={"event_id": "$reconciled", "room_id": "!room:test"},
    )
    assert repeated.status is ActionIntentStatus.CONFIRMED


@pytest.mark.asyncio
@pytest.mark.parametrize("reconciler", [None, AsyncMock(return_value=None)])
async def test_unknown_action_replay_stays_quarantined_without_a_provider_receipt(reconciler):
    """A missing reconciliation receipt must never permit another provider write."""
    handler = AsyncMock(return_value={"error": "provider response was lost"})
    action = _transactional_action(handler)
    assert action.action_intent is not None
    action = replace(
        action,
        action_intent=replace(action.action_intent, reconcile_unknown=reconciler),
    )
    request_id = "request-reconcile-no-receipt"
    await _prepared_approved_intent(action, request_id)

    await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id=request_id,
    )
    intent, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )

    assert intent is not None
    assert intent.status is ActionIntentStatus.OUTCOME_UNKNOWN
    assert replay == {
        "error": "External action outcome is unknown; reconcile the provider before retrying."
    }
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_action_replay_survives_reconciliation_read_failure():
    handler = AsyncMock(return_value={"error": "provider response was lost"})
    action = _transactional_action(handler)
    assert action.action_intent is not None
    reconciler = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    action = replace(
        action,
        action_intent=replace(action.action_intent, reconcile_unknown=reconciler),
    )
    request_id = "request-reconcile-read-failure"
    await _prepared_approved_intent(action, request_id)
    await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id=request_id,
    )

    intent, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )

    assert intent is not None
    assert intent.status is ActionIntentStatus.OUTCOME_UNKNOWN
    assert replay == {
        "error": "External action outcome is unknown; reconcile the provider before retrying."
    }
    reconciler.assert_awaited_once()
