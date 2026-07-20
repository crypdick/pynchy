"""Behavioral coverage for durable, fail-closed external action intents."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy.action_intents import ActionIntentStatus
from pynchy.actions import ActionId
from pynchy.capabilities import (
    ActionIntentContract,
    ActionIntentDraft,
    ActionIntentReceipt,
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    HostActionAccess,
    HostActionDescriptor,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.host.container_manager.action_intents import (
    execute_action_intent,
    prepare_action_intent,
)
from pynchy.host.container_manager.ipc import registry
from pynchy.host.container_manager.ipc.handlers_approval import process_approval_decision
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate
from pynchy.plugins.host_actions import HostActionCatalog
from pynchy.plugins.integrations.matrix_gateway import MATRIX_HOST_ACTIONS
from pynchy.state import (
    action_intent_to_dict,
    approve_action_intent,
    claim_action_intent,
    expire_action_intent,
    get_action_intent_by_request,
    init_test_database,
    mark_action_intent_executing,
    recover_incomplete_action_intents,
)
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity


@pytest.fixture(autouse=True)
async def _database():
    await init_test_database()


def _transactional_action(handler: AsyncMock) -> HostActionDescriptor:
    def draft_from_request(data: dict[str, object]) -> ActionIntentDraft:
        room_id = data["room_id"]
        body = data["body"]
        if not isinstance(room_id, str) or not isinstance(body, str):
            raise TypeError("room_id and body must be strings")
        return ActionIntentDraft(
            recipient=room_id,
            payload={"room_id": room_id, "body": body},
            summary=f"Send test message to {room_id}",
        )

    def receipt_from_response(response: dict[str, object]) -> ActionIntentReceipt:
        raw_result = response.get("result")
        if not isinstance(raw_result, dict):
            raise TypeError("test provider result is missing")
        event_id = raw_result.get("event_id")
        if not isinstance(event_id, str):
            raise TypeError("test provider event ID is missing")
        return ActionIntentReceipt(provider_request_id=event_id, receipt=raw_result)

    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId("test.transactional.send"),
            kind=CapabilityKind.HOST_ACTION,
            owner="tests",
            summary="Send a transactional test message.",
            action_ids=(ActionId("chat.matrix.message.send"),),
        ),
        tool_name=HostToolName("test_transactional_send"),
        handler=handler,
        access=HostActionAccess.WRITE,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(IdempotencyMode.IPC_REQUEST_ID),
        audit=AuditContract(),
        action_intent=ActionIntentContract(
            provider="test-provider",
            draft_from_request=draft_from_request,
            receipt_from_response=receipt_from_response,
        ),
    )


@pytest.mark.asyncio
async def test_human_approval_executes_exactly_one_transactional_provider_call(tmp_path):
    handler = AsyncMock(return_value={"result": {"event_id": "$approved", "room_id": "!room:test"}})
    action = _transactional_action(handler)
    request_id = "request-approved"
    settings = make_settings(data_dir=tmp_path)
    gate = create_gate(
        "test-workspace",
        1.0,
        WorkspaceSecurity(
            services={
                "test_transactional_send": ServiceTrustConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=True,
                )
            }
        ),
    )
    request = {
        "type": "service:test_transactional_send",
        "request_id": request_id,
        "room_id": "!room:test",
        "body": "private payload",
    }
    decision_path = (
        tmp_path / "ipc" / "test-workspace" / "approval_decisions" / f"{request_id}.json"
    )
    try:
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_service.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
                return_value=HostActionCatalog(actions=(action,)),
            ),
            patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
            patch(
                "pynchy.host.container_manager.security.approval.get_settings",
                return_value=settings,
            ),
        ):
            await registry.dispatch(request, "test-workspace", False, NullIpcDeps())

        intent = await get_action_intent_by_request(request_id)
        assert intent is not None
        assert intent.status is ActionIntentStatus.AWAITING_APPROVAL
        pending = json.loads(
            (
                settings.data_dir / "ipc/test-workspace/pending_approvals" / f"{request_id}.json"
            ).read_text(encoding="utf-8")
        )
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "guarded_action_id": pending["guarded_action_id"],
                    "request_payload_hash": pending["request_payload_hash"],
                    "source_group": pending["source_group"],
                    "approved": True,
                    "decided_by": "test-user",
                    "decided_at": "2026-07-18T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval._get_action_catalog",
                return_value=HostActionCatalog(actions=(action,)),
            ),
            patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        ):
            await process_approval_decision(decision_path, "test-workspace")
    finally:
        destroy_gate("test-workspace", 1.0)

    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.CONFIRMED
    assert intent.approver == "test-user"
    handler.assert_awaited_once()


async def _prepared_approved_intent(action: HostActionDescriptor, request_id: str) -> None:
    intent, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )
    assert intent is not None
    assert replay is None
    await approve_action_intent(
        request_id,
        approver="test-user",
        approved_at="2026-07-18T12:00:00+00:00",
        policy_decision="approved by test",
    )


@pytest.mark.asyncio
async def test_receipted_action_replay_returns_receipt_without_resending():
    handler = AsyncMock(return_value={"result": {"event_id": "$event", "room_id": "!room:test"}})
    action = _transactional_action(handler)
    request_id = "request-receipt"
    await _prepared_approved_intent(action, request_id)
    resumed, resumed_replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )
    assert resumed is not None
    assert resumed.status is ActionIntentStatus.APPROVED
    assert resumed_replay is None
    assert (
        await approve_action_intent(
            request_id,
            approver="test-user",
            approved_at="2026-07-18T12:00:00+00:00",
            policy_decision="approved by test",
        )
        == resumed
    )

    first = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id=request_id,
    )
    replay = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id=request_id,
    )

    assert first == {"result": {"event_id": "$event", "room_id": "!room:test"}}
    assert replay == {
        "result": json.dumps({"event_id": "$event", "room_id": "!room:test"}, sort_keys=True)
    }
    handler.assert_awaited_once()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.CONFIRMED
    assert intent.provider_request_id == "$event"
    assert "body" not in action_intent_to_dict(intent)


@pytest.mark.asyncio
async def test_provider_failure_becomes_unknown_and_never_retries():
    handler = AsyncMock(side_effect=RuntimeError("gateway connection lost"))
    action = _transactional_action(handler)
    request_id = "request-unknown"
    await _prepared_approved_intent(action, request_id)

    first = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id=request_id,
    )
    replay = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id=request_id,
    )

    assert first["error"] == "External action outcome is unknown; do not retry automatically."
    assert replay["error"] == (
        "External action outcome is unknown; reconcile the provider before retrying."
    )
    handler.assert_awaited_once()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_startup_recovery_marks_claimed_provider_call_unknown():
    handler = AsyncMock(return_value={"result": {"event_id": "$unused"}})
    action = _transactional_action(handler)
    request_id = "request-recovery"
    await _prepared_approved_intent(action, request_id)
    claimed = await claim_action_intent(request_id)
    assert claimed is not None
    await mark_action_intent_executing(request_id)

    assert await recover_incomplete_action_intents() == 1
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.OUTCOME_UNKNOWN
    assert "reconcile" in (intent.error or "")


@pytest.mark.asyncio
async def test_expired_approval_closes_unexecuted_action_intent():
    action = _transactional_action(AsyncMock())
    request_id = "request-expired"
    intent, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )
    assert intent is not None
    assert replay is None

    expired = await expire_action_intent(request_id, reason="Approval elapsed")

    assert expired is not None
    assert expired.status is ActionIntentStatus.EXPIRED


def test_matrix_send_declares_canonical_draft_and_event_receipt():
    action = MATRIX_HOST_ACTIONS.action_for("matrix_send_message")
    assert action is not None
    assert action.action_intent is not None

    draft = action.action_intent.draft_from_request(
        {"room_id": "!friend:matrix.example.com", "body": "hello"}
    )
    receipt = action.action_intent.receipt_from_response(
        {"result": '{"event_id":"$event","room_id":"!friend:matrix.example.com"}'}
    )

    assert draft.payload == {"room_id": "!friend:matrix.example.com", "body": "hello"}
    assert receipt.provider_request_id == "$event"
