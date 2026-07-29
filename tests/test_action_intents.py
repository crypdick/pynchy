"""Behavioral coverage for durable, fail-closed external action intents."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

import pynchy.host.container_manager.ipc.registry as registry
from pynchy.action_intents import ActionIntentStatus
from pynchy.conversation.models import (
    ConversationId,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.container_manager.ipc.handlers_approval import process_approval_decision
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate
from pynchy.host.orchestrator.api import (
    execute_action_intent,
    prepare_action_intent,
)
from pynchy.identifiers import (
    ChatJid,
)
from pynchy.plugins.api import (
    HostActionCatalog,
)
from pynchy.state import (
    action_intent_to_dict,
    approve_action_intent,
    claim_action_intent,
    expire_action_intent,
    get_action_intent_by_request,
    mark_action_intent_awaiting_approval,
    mark_action_intent_executing,
    recover_incomplete_action_intents,
)
from pynchy.workspace.api import (
    ServiceTrustConfig,
    WorkspaceSecurity,
)
from tests.action_intents_support import (
    _bind_matrix_route,
    _matrix_control_binding,
    _prepared_approved_intent,
    _process_matrix_approval,
    _transactional_action,
    _write_matrix_approval,
)

pytest_plugins = ("tests.action_intents_support",)

if TYPE_CHECKING:
    from pathlib import Path


_MATRIX_ROOM = "!family:matrix.example.com"

_MATRIX_CONTROL = ChatJid("discord:channel:matrix-family")

_MATRIX_CONVERSATION = ConversationId("conv_family")

_MATRIX_FOLDER = routed_conversation_folder("support", _MATRIX_CONVERSATION)


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
        tmp_path / "approvals" / "test-workspace" / "approval_decisions" / f"{request_id}.json"
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
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch(
                "pynchy.host.container_manager.security.approval._approval_root",
                settings.data_dir / "approvals",
            ),
        ):
            await registry.dispatch(request, "test-workspace", False, NullIpcDeps())

        intent = await get_action_intent_by_request(request_id)
        assert intent is not None
        assert intent.status is ActionIntentStatus.AWAITING_APPROVAL
        pending = json.loads(
            (
                settings.data_dir
                / "approvals/test-workspace/pending_approvals"
                / f"{request_id}.json"
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
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=HostActionCatalog(actions=(action,)),
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await process_approval_decision(
                decision_path,
                "test-workspace",
                deps=NullIpcDeps(),
            )
    finally:
        destroy_gate("test-workspace", 1.0)

    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.CONFIRMED
    assert intent.approver == "test-user"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_matrix_approval_never_reaches_provider(tmp_path: Path) -> None:
    request_id = "matrix-expired"
    action, provider, _pending, decision = await _write_matrix_approval(
        tmp_path,
        request_id=request_id,
        timestamp=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )

    response = await _process_matrix_approval(tmp_path, action, decision)

    provider.assert_not_awaited()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.FAILED
    assert "expired" in str(response["error"]).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampering",
    ["payload_hash", "durable_intent", "pending_conversation", "pending_chat"],
)
async def test_tampered_matrix_approval_evidence_never_reaches_provider(
    tmp_path: Path,
    tampering: str,
) -> None:
    request_id = f"matrix-tampered-{tampering}"
    action, provider, pending, decision = await _write_matrix_approval(
        tmp_path,
        request_id=request_id,
    )
    pending_path = decision.parents[1] / "pending_approvals" / decision.name
    if tampering == "payload_hash":
        pending["action_payload_sha256"] = "0" * 64
    elif tampering == "durable_intent":
        altered = dict(pending["action_payload"])
        altered["body"] = "replacement payload"
        pending["action_payload"] = altered
        pending["action_payload_sha256"] = hashlib.sha256(
            json.dumps(altered, sort_keys=True).encode()
        ).hexdigest()
    elif tampering == "pending_conversation":
        pending["origin_conversation_id"] = "different-conversation"
    else:
        pending["approval_chat_jid"] = "discord:channel:different-thread"
    pending_path.write_text(json.dumps(pending), encoding="utf-8")

    response = await _process_matrix_approval(tmp_path, action, decision)

    provider.assert_not_awaited()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.FAILED
    assert any(
        word in str(response["error"]).lower()
        for word in ("payload", "intent", "conversation", "destination")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_change", ["missing", "replaced"])
async def test_changed_matrix_control_binding_never_reaches_provider(
    tmp_path: Path,
    binding_change: str,
) -> None:
    request_id = f"matrix-binding-{binding_change}"
    action, provider, _pending, decision = await _write_matrix_approval(
        tmp_path,
        request_id=request_id,
    )
    replacement = _matrix_control_binding(thread_jid=ChatJid("discord:channel:replacement"))

    response = await _process_matrix_approval(
        tmp_path,
        action,
        decision,
        binding=replacement,
        binding_missing=binding_change == "missing",
    )

    provider.assert_not_awaited()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.FAILED
    assert "binding changed" in str(response["error"])


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_change", ["route_removed", "tool_removed", "read_only"])
async def test_matrix_approval_rechecks_current_route_policy_before_provider(
    tmp_path: Path,
    policy_change: str,
) -> None:
    request_id = f"matrix-policy-{policy_change}"
    action, provider, _pending, decision = await _write_matrix_approval(
        tmp_path,
        request_id=request_id,
    )
    if policy_change == "read_only":
        _bind_matrix_route(outbound="read_only")

    response = await _process_matrix_approval(
        tmp_path,
        action,
        decision,
        resolved_tools=(["matrix_route_read"] if policy_change == "tool_removed" else None),
        policy_available=policy_change != "route_removed",
    )

    provider.assert_not_awaited()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.FAILED
    assert "error" in response


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
async def test_prepare_replays_awaiting_approval_without_creating_a_second_intent():
    action = _transactional_action(AsyncMock())
    request_id = "request-awaiting-approval"
    intent, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )
    assert intent is not None
    assert replay is None
    await mark_action_intent_awaiting_approval(request_id, policy_decision="human required")

    replayed, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )

    assert replayed is not None
    assert replayed.id == intent.id
    assert replay == {"error": "External action is awaiting human approval."}


@pytest.mark.asyncio
async def test_invalid_action_arguments_do_not_create_a_durable_intent():
    handler = AsyncMock()
    action = _transactional_action(handler)

    intent, response = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": 1},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id="request-invalid-arguments",
    )

    assert intent is None
    assert response == {
        "error": "Invalid external action arguments: room_id and body must be strings"
    }
    assert await get_action_intent_by_request("request-invalid-arguments") is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_response",
    [{"error": "provider rejected the request"}, {"result": {}}],
)
async def test_unreceipted_provider_responses_become_outcome_unknown(
    provider_response: dict[str, object],
):
    handler = AsyncMock(return_value=provider_response)
    action = _transactional_action(handler)
    request_id = f"request-unreceipted-{len(provider_response)}"
    await _prepared_approved_intent(action, request_id)

    response = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id=request_id,
    )

    assert response == {"error": "External action outcome is unknown; do not retry automatically."}
    handler.assert_awaited_once()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_invalid_approved_payload_fails_before_provider_execution():
    handler = AsyncMock()
    action = _transactional_action(handler)
    request_id = "request-invalid-approved-payload"
    await _prepared_approved_intent(action, request_id)

    response = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": 1},
        request_id=request_id,
    )

    assert response == {"error": "Approved external action is no longer valid; request it again."}
    handler.assert_not_awaited()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.FAILED


@pytest.mark.asyncio
async def test_missing_durable_intent_refuses_provider_execution():
    handler = AsyncMock()
    action = _transactional_action(handler)

    response = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        request_id="request-missing-durable-intent",
    )

    assert response == {"error": "External action record is missing; refusing to send."}
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_approved_payload_fails_before_provider_execution():
    handler = AsyncMock()
    action = _transactional_action(handler)
    request_id = "request-changed-approved-payload"
    await _prepared_approved_intent(action, request_id)

    response = await execute_action_intent(
        action,
        {"room_id": "!room:test", "body": "changed payload"},
        request_id=request_id,
    )

    assert response == {"error": "Approved external action changed; request a new approval."}
    handler.assert_not_awaited()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.FAILED


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

    replayed, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "private payload"},
        workspace="test-workspace",
        chat_jid="test@g.us",
        request_id=request_id,
    )

    assert replayed == expired
    assert replay == {"error": "Approval elapsed"}
