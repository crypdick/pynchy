"""Behavioral coverage for durable, fail-closed external action intents."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy.actions.api import ActionId
from pynchy.config.api import MatrixConnectionConfig, MatrixEndpointConfig
from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.container_manager.ipc.handlers_approval import process_approval_decision
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.host.orchestrator.api import (
    prepare_action_intent,
)
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
)
from pynchy.plugins.api import (
    ActionIntentContract,
    ActionIntentDraft,
    ActionIntentReceipt,
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    HostActionAccess,
    HostActionCatalog,
    HostActionDescriptor,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.plugins.integrations import matrix_gateway
from pynchy.plugins.integrations.matrix_gateway_client import MatrixPortalAssertion
from pynchy.plugins.integrations.matrix_route_registry import (
    ActiveMatrixRoute,
    bind_active_matrix_route,
    clear_active_matrix_routes,
)
from pynchy.plugins.integrations.matrix_route_resolution import ResolvedMatrixRoute
from pynchy.state import (
    approve_action_intent,
    init_test_database,
    mark_action_intent_awaiting_approval,
)
from pynchy.workspace.api import (
    ResolvedWorkspaceConfig,
    WorkspaceSecurity,
)
from tests.approval_support import write_encrypted_pending_approval

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
    _pending_path, pending = write_encrypted_pending_approval(
        tmp_path / "approvals",
        request_id=request_id,
        tool_name="matrix_route_send",
        source_group=_MATRIX_FOLDER,
        approval_chat_jid=str(_MATRIX_CONTROL),
        request_data=request_data,
        handler_type="service",
        expires_after_seconds=300,
        origin_conversation_id=str(_MATRIX_CONVERSATION),
        action_payload=intent.payload,
        timestamp=timestamp,
    )
    decision = {
        "request_id": request_id,
        "guarded_action_id": pending["guarded_action_id"],
        "request_payload_hash": pending["request_payload_hash"],
        "source_group": pending["source_group"],
        "approved": True,
        "decided_by": "operator",
        "decided_at": datetime.now(UTC).isoformat(),
    }
    decision_path = (
        tmp_path / "approvals" / _MATRIX_FOLDER / "approval_decisions" / f"{request_id}.json"
    )
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    return action, provider_handler, pending, decision_path


def _resolved_matrix_workspace(*, tools: list[str] | None = None) -> ResolvedWorkspaceConfig:
    return ResolvedWorkspaceConfig(
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
    deps = NullIpcDeps()
    deps.get_conversation_control_binding = AsyncMock(
        return_value=(
            None
            if binding_missing
            else binding
            if binding is not None
            else _matrix_control_binding()
        )
    )
    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
            return_value=settings,
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
            return_value=HostActionCatalog(actions=(action,)),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
            return_value=SecurityGate(WorkspaceSecurity()) if policy_available else None,
        ),
        patch(
            "pynchy.config.settings.Settings.resolved_workspace_config",
            return_value=(
                _resolved_matrix_workspace(tools=resolved_tools) if policy_available else None
            ),
        ),
    ):
        await process_approval_decision(
            decision_path,
            _MATRIX_FOLDER,
            deps=deps,
        )
    response_path = tmp_path / "ipc" / _MATRIX_FOLDER / "responses" / decision_path.name
    return json.loads(response_path.read_text(encoding="utf-8"))


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
