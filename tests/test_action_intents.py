"""Behavioral coverage for durable, fail-closed external action intents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
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
from pynchy.config.merge import ResolvedWorkspaceConfig
from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.container_manager.action_intents import (
    execute_action_intent,
    prepare_action_intent,
)
from pynchy.host.container_manager.ipc import registry
from pynchy.host.container_manager.ipc.handlers_approval import process_approval_decision
from pynchy.host.container_manager.security.gate import SecurityGate, create_gate, destroy_gate
from pynchy.plugins.host_actions import HostActionCatalog
from pynchy.plugins.integrations import matrix_gateway
from pynchy.plugins.integrations.matrix_gateway_client import MatrixPortalAssertion
from pynchy.plugins.integrations.matrix_route_registry import (
    ActiveMatrixRoute,
    bind_active_matrix_route,
    clear_active_matrix_routes,
)
from pynchy.plugins.integrations.matrix_route_resolution import ResolvedMatrixRoute
from pynchy.plugins.integrations.matrix_routing_config import (
    MatrixConnectionConfig,
    MatrixEndpointConfig,
)
from pynchy.state import (
    action_intent_to_dict,
    approve_action_intent,
    claim_action_intent,
    expire_action_intent,
    get_action_intent_by_request,
    init_test_database,
    mark_action_intent_awaiting_approval,
    mark_action_intent_executing,
    recover_incomplete_action_intents,
)
from pynchy.types import ChatJid, GroupFolder, ServiceTrustConfig, WorkspaceSecurity

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
async def _database():
    await init_test_database()
    clear_active_matrix_routes()
    yield
    clear_active_matrix_routes()


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
            action_ids=(ActionId("chat.matrix.route.send"),),
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


_MATRIX_ROOM = "!family:matrix.example.com"
_MATRIX_CONTROL = ChatJid("discord:channel:matrix-family")
_MATRIX_CONVERSATION = ConversationId("conv_family")
_MATRIX_FOLDER = routed_conversation_folder("support", _MATRIX_CONVERSATION)


def _matrix_route(*, outbound: str = "approval_required") -> ResolvedMatrixRoute:
    connection = MatrixConnectionConfig(
        expected_user_id="@me:matrix.example.com",
        chat={"family": MatrixEndpointConfig(room_id=_MATRIX_ROOM, title="Family")},
    )
    return ResolvedMatrixRoute(
        name="family",
        connection_name="personal-chats",
        connection=connection,
        endpoint_name="family",
        endpoint=connection.chat["family"],
        control_title="Family",
        workspace="support",
        activation="on_event",
        outbound=outbound,  # type: ignore[arg-type]
        tools=("matrix_route_read", "matrix_route_send"),
        capabilities={},
    )


def _bind_matrix_route(*, outbound: str = "approval_required") -> None:
    bind_active_matrix_route(
        ActiveMatrixRoute(
            workspace_folder=_MATRIX_FOLDER,
            conversation_id=_MATRIX_CONVERSATION,
            control_thread_jid=_MATRIX_CONTROL,
            route=_matrix_route(outbound=outbound),
            portal=MatrixPortalAssertion(
                room_id=_MATRIX_ROOM,
                owner_user_id="@me:matrix.example.com",
                joined=True,
            ),
        )
    )


def _matrix_control_binding(*, thread_jid: ChatJid = _MATRIX_CONTROL) -> ConversationControlBinding:
    return ConversationControlBinding(
        conversation_id=_MATRIX_CONVERSATION,
        surface=ControlSurface.DISCORD,
        parent_workspace=GroupFolder("support"),
        parent_jid=ChatJid("discord:channel:support"),
        thread_jid=thread_jid,
        title="Family",
        updated_at=datetime.now(UTC).isoformat(),
    )


async def _write_matrix_approval(
    tmp_path,
    *,
    request_id: str,
    timestamp: str | None = None,
) -> tuple[HostActionDescriptor, AsyncMock, dict[str, object], Path]:
    _bind_matrix_route()
    original = matrix_gateway.MATRIX_HOST_ACTIONS.action_for("matrix_route_send")
    assert original is not None
    provider_handler = AsyncMock(return_value={"result": "provider must not run"})
    action = replace(original, handler=provider_handler)
    request_data: dict[str, object] = {
        "type": "service:matrix_route_send",
        "request_id": request_id,
        "source_group": _MATRIX_FOLDER,
        "body": "private reply",
    }
    intent, replay = await prepare_action_intent(
        action,
        request_data,
        workspace=_MATRIX_FOLDER,
        chat_jid=str(_MATRIX_CONTROL),
        request_id=request_id,
    )
    assert replay is None
    assert intent is not None
    await mark_action_intent_awaiting_approval(request_id, policy_decision="human required")
    pending = {
        "request_id": request_id,
        "short_id": "mx",
        "tool_name": "matrix_route_send",
        "source_group": _MATRIX_FOLDER,
        "approval_chat_jid": str(_MATRIX_CONTROL),
        "origin_conversation_id": str(_MATRIX_CONVERSATION),
        "handler_type": "service",
        "request_data": request_data,
        "action_payload": intent.payload,
        "action_payload_sha256": hashlib.sha256(
            json.dumps(intent.payload, sort_keys=True).encode()
        ).hexdigest(),
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "expires_after_seconds": 300,
    }
    pending_path = tmp_path / "ipc" / _MATRIX_FOLDER / "pending_approvals" / f"{request_id}.json"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    decision = {
        "request_id": request_id,
        "approved": True,
        "decided_by": "operator",
        "decided_at": datetime.now(UTC).isoformat(),
    }
    decision_path = tmp_path / "ipc" / _MATRIX_FOLDER / "approval_decisions" / f"{request_id}.json"
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    return action, provider_handler, pending, decision_path


def _resolved_matrix_workspace(*, tools: list[str] | None = None) -> ResolvedWorkspaceConfig:
    return ResolvedWorkspaceConfig(
        prompts=[],
        skills=[],
        tools=tools if tools is not None else ["matrix_route_read", "matrix_route_send"],
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
    )


async def _process_matrix_approval(
    tmp_path: Path,
    action: HostActionDescriptor,
    decision_path: Path,
    *,
    resolved_tools: list[str] | None = None,
    binding: ConversationControlBinding | None = None,
    binding_missing: bool = False,
    policy_available: bool = True,
) -> dict[str, object]:
    settings = make_settings(data_dir=tmp_path)
    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
            return_value=settings,
        ),
        patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.ipc.handlers_approval._get_action_catalog",
            return_value=HostActionCatalog(actions=(action,)),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_approval._approval_replay_gate",
            return_value=SecurityGate(WorkspaceSecurity()) if policy_available else None,
        ),
        patch(
            "pynchy.host.container_manager.ipc.approval_replay."
            "workspace_config.load_resolved_config",
            return_value=(
                _resolved_matrix_workspace(tools=resolved_tools) if policy_available else None
            ),
        ),
        patch(
            "pynchy.host.container_manager.ipc.approval_replay.get_conversation_control_binding",
            new_callable=AsyncMock,
            return_value=(
                None
                if binding_missing
                else binding
                if binding is not None
                else _matrix_control_binding()
            ),
        ),
    ):
        await process_approval_decision(decision_path, _MATRIX_FOLDER)
    response_path = tmp_path / "ipc" / _MATRIX_FOLDER / "responses" / decision_path.name
    return json.loads(response_path.read_text(encoding="utf-8"))


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
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(
            json.dumps(
                {
                    "request_id": request_id,
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
@pytest.mark.parametrize("tampering", ["payload_hash", "durable_intent"])
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
    else:
        altered = dict(pending["action_payload"])
        altered["body"] = "replacement payload"
        pending["action_payload"] = altered
        pending["action_payload_sha256"] = hashlib.sha256(
            json.dumps(altered, sort_keys=True).encode()
        ).hexdigest()
    pending_path.write_text(json.dumps(pending), encoding="utf-8")

    response = await _process_matrix_approval(tmp_path, action, decision)

    provider.assert_not_awaited()
    intent = await get_action_intent_by_request(request_id)
    assert intent is not None
    assert intent.status is ActionIntentStatus.FAILED
    assert any(word in str(response["error"]).lower() for word in ("payload", "intent"))


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
