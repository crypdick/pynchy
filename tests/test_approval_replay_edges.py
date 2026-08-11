"""Fail-closed validation coverage for approval replay evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from conftest import NullIpcDeps

from pynchy.conversation.api import ConversationId
from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalDecisionContext,
    ApprovalReplayPolicy,
    approval_replay_gate,
    approval_replay_validation_error,
)
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.state import ActionIntentCreateRequest, create_action_intent, init_test_database
from pynchy.workspace.api import WorkspaceSecurity


def _context(**overrides: object) -> ApprovalDecisionContext:
    now = datetime.now(UTC).isoformat()
    values: dict[str, object] = {
        "request_id": "request",
        "source_group": "group",
        "tool_name": "tool",
        "chat_jid": "j@g.us",
        "request_data": {},
        "approved": True,
        "approver": "operator",
        "approved_at": now,
        "handler_type": "ipc",
        "action": None,
        "gate": None,
        "capability_id": None,
        "action_ids": (),
        "origin_conversation_id": None,
        "action_payload": None,
        "action_payload_sha256": None,
        "requested_at": now,
        "expires_after_seconds": 300,
    }
    values.update(overrides)
    return ApprovalDecisionContext(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_at", "expected"),
    [
        (None, "approval request has no timestamp"),
        ("not-a-timestamp", "approval request timestamp is invalid"),
        ("2026-07-29T00:00:00", "approval request timestamp has no timezone"),
    ],
)
async def test_replay_rejects_invalid_approval_timestamps(
    requested_at: str | None, expected: str
) -> None:
    error = await approval_replay_validation_error(
        _context(requested_at=requested_at),
        NullIpcDeps(),
        ApprovalReplayPolicy(
            configured_security=lambda _group: WorkspaceSecurity(),
            workspace_tools=lambda _group: (),
        ),
    )

    assert error == expected


@pytest.mark.asyncio
async def test_replay_rejects_removed_tool_from_routed_workspace() -> None:
    error = await approval_replay_validation_error(
        _context(
            handler_type="service",
            gate=SecurityGate(WorkspaceSecurity()),
            origin_conversation_id=ConversationId("conversation"),
        ),
        NullIpcDeps(),
        ApprovalReplayPolicy(
            configured_security=lambda _group: WorkspaceSecurity(),
            workspace_tools=lambda _group: None,
        ),
    )

    assert error == "host tool is no longer enabled for this routed workspace"


@pytest.mark.asyncio
async def test_replay_rejects_route_bound_payload_without_origin() -> None:
    await init_test_database()
    payload = {"conversation_id": "conv", "approval_chat_jid": "j@g.us"}
    await create_action_intent(
        ActionIntentCreateRequest(
            request_id="request",
            workspace="group",
            action_id="action",
            tool_name="tool",
            provider="provider",
            actor_jid="actor",
            recipient="recipient",
            payload=payload,
            source_refs=(),
            summary="summary",
        )
    )
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    error = await approval_replay_validation_error(
        _context(
            action_payload=payload,
            action_payload_sha256=payload_hash,
            expires_after_seconds=999999,
        ),
        NullIpcDeps(),
        ApprovalReplayPolicy(
            configured_security=lambda _group: WorkspaceSecurity(),
            workspace_tools=lambda _group: (),
        ),
    )

    assert error == "route-bound approval lost its originating conversation"


def test_replay_gate_handles_unresolved_and_unconfigured_policy() -> None:
    with patch(
        "pynchy.host.container_manager.ipc.approval_replay.get_gate_for_group",
        return_value=None,
    ):
        assert (
            approval_replay_gate(
                "group",
                policy=ApprovalReplayPolicy(
                    configured_security=lambda _group: WorkspaceSecurity(),
                    workspace_tools=lambda _group: None,
                ),
                require_resolved=True,
            )
            is None
        )
        assert (
            approval_replay_gate(
                "group",
                policy=ApprovalReplayPolicy(
                    configured_security=lambda _group: None,
                    workspace_tools=lambda _group: ("tool",),
                ),
            )
            is None
        )
        gate = approval_replay_gate(
            "group",
            policy=ApprovalReplayPolicy(
                configured_security=lambda _group: WorkspaceSecurity(),
                workspace_tools=lambda _group: ("tool",),
            ),
        )

    assert isinstance(gate, SecurityGate)
