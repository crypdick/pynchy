"""Tests for cop_gate host-mutating operation gating."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.container_manager.ipc.deps import IpcDeps
from pynchy.host.container_manager.security.cop import (
    CopContextAvailability,
    CopInspectionContext,
    CopVerdict,
)
from pynchy.host.container_manager.security.cop_gate import cop_gate
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.types import OutboundEventType, WorkspaceSecurity


@pytest.fixture
def mock_deps():
    deps = MagicMock(spec=IpcDeps)
    deps.workspaces.return_value = {"jid-1": MagicMock(folder="admin-1")}
    deps.broadcast_to_channels = AsyncMock()
    deps.broadcast_host_message = AsyncMock()
    deps.load_cop_inspection_context = AsyncMock(
        return_value=CopInspectionContext(availability=CopContextAvailability.AVAILABLE)
    )
    return deps


@pytest.mark.asyncio
async def test_cop_allows_clean_operation(mock_deps):
    """Clean operation passes through — cop_gate returns True."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            return_value=CopVerdict(flagged=False),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ),
    ):
        result = await cop_gate(
            "sync_worktree_to_main",
            "diff: fix typo",
            {"type": "sync_worktree_to_main"},
            "admin-1",
            mock_deps,
        )
    assert result is True


@pytest.mark.asyncio
async def test_missing_inspection_evidence_forces_human_without_model_guess(
    mock_deps,
) -> None:
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            new_callable=AsyncMock,
        ) as inspect,
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ) as audit,
        patch(
            "pynchy.host.container_manager.security.cop_gate.create_pending_approval",
            return_value="a1",
        ) as create_pending,
    ):
        allowed = await cop_gate(
            "sync_worktree_to_main",
            "publish committed worktree",
            {"type": "sync_worktree_to_main", "request_id": "guard-patch"},
            "admin-1",
            mock_deps,
            request_id="guard-patch",
            required_human_reason="Committed patch exceeds the inspection limit",
        )

    assert allowed is False
    inspect.assert_not_awaited()
    create_pending.assert_called_once()
    assert audit.await_args.kwargs["decision"] == "cop_degraded"
    assert audit.await_args.kwargs["reason"] == ("Committed patch exceeds the inspection limit")


@pytest.mark.asyncio
async def test_inactive_cop_skips_outbound_inspection(mock_deps):
    """A profile can omit Cop without weakening explicit human contracts."""
    gate = SecurityGate(WorkspaceSecurity(cop_active=False))
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.get_gate_for_group",
            return_value=gate,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            new_callable=AsyncMock,
        ) as inspect,
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ) as audit,
    ):
        result = await cop_gate(
            "sync_worktree_to_main",
            "diff: bounded repair",
            {"type": "sync_worktree_to_main"},
            "admin-1",
            mock_deps,
        )

    assert result is True
    inspect.assert_not_awaited()
    assert audit.await_args.kwargs["decision"] == "cop_disabled_by_profile"


@pytest.mark.asyncio
async def test_cop_blocks_flagged_with_request_id(mock_deps):
    """Flagged operation with request_id creates pending approval and returns False."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            return_value=CopVerdict(flagged=True, reason="suspicious"),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.create_pending_approval",
            return_value="a1",
        ) as mock_create,
    ):
        result = await cop_gate(
            "sync_worktree_to_main",
            "diff: add backdoor",
            {"type": "sync_worktree_to_main", "request_id": "req-123"},
            "admin-1",
            mock_deps,
            request_id="req-123",
        )

    assert result is False
    mock_create.assert_called_once()
    # Verify handler_type="ipc" was passed
    call_kwargs = mock_create.call_args
    assert call_kwargs.kwargs.get("handler_type") == "ipc" or (
        len(call_kwargs.args) > 5 and call_kwargs.args[5] == "ipc"
    )
    event = mock_deps.broadcast_to_channels.call_args.args[1]
    assert event.type is OutboundEventType.APPROVAL


@pytest.mark.asyncio
async def test_cop_blocks_flagged_fire_and_forget(mock_deps):
    """Flagged fire-and-forget operation broadcasts warning, no approval."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            return_value=CopVerdict(flagged=True, reason="suspicious"),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ),
    ):
        result = await cop_gate(
            "register_group",
            "name=evil, folder=evil",
            {"type": "register_group"},
            "admin-1",
            mock_deps,
            # No request_id — fire-and-forget
        )

    assert result is False
    mock_deps.broadcast_to_channels.assert_called_once()


@pytest.mark.asyncio
async def test_cop_gate_resolves_chat_jid(mock_deps):
    """cop_gate resolves chat_jid from deps.workspaces() for audit logging."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            return_value=CopVerdict(flagged=False),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        await cop_gate(
            "sync_worktree_to_main",
            "diff: fix typo",
            {"type": "sync_worktree_to_main"},
            "admin-1",
            mock_deps,
        )

    # Should have resolved jid-1 from the workspace mapping
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["chat_jid"] == "jid-1"


@pytest.mark.asyncio
async def test_cop_gate_unknown_group_uses_fallback_jid(mock_deps):
    """When source_group is not found in workspaces, uses 'unknown' as chat_jid."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            return_value=CopVerdict(flagged=False),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        await cop_gate(
            "sync_worktree_to_main",
            "diff: fix typo",
            {"type": "sync_worktree_to_main"},
            "nonexistent-group",  # Not in workspaces
            mock_deps,
        )

    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs["chat_jid"] == "unknown"


@pytest.mark.asyncio
async def test_cop_gate_notification_includes_reason(mock_deps):
    """When flagged with request_id, notification message includes cop reason."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            return_value=CopVerdict(flagged=True, reason="backdoor pattern detected"),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.create_pending_approval",
            return_value="a1",
        ),
    ):
        await cop_gate(
            "sync_worktree_to_main",
            "diff: add backdoor",
            {"type": "sync_worktree_to_main", "request_id": "req-456"},
            "admin-1",
            mock_deps,
            request_id="req-456",
        )

    # Notification should contain the cop reason
    broadcast_call = mock_deps.broadcast_to_channels.call_args
    event = broadcast_call.args[1]
    assert event.type is OutboundEventType.APPROVAL
    assert "backdoor pattern detected" in event.content


@pytest.mark.asyncio
async def test_missing_context_requires_human_approval(mock_deps):
    """Request-reply operations escalate when bounded context is unavailable."""
    unavailable = CopInspectionContext(
        availability=CopContextAvailability.UNAVAILABLE,
        unavailable_reason="database unavailable",
    )
    mock_deps.load_cop_inspection_context.return_value = unavailable
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=False),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.create_pending_approval",
            return_value="a1",
        ) as create_pending,
    ):
        allowed = await cop_gate(
            "sync_worktree_to_main",
            "diff: fix typo",
            {"type": "sync_worktree_to_main", "request_id": "guard-1"},
            "admin-1",
            mock_deps,
            request_id="guard-1",
        )

    assert allowed is False
    create_pending.assert_called_once()
    assert mock_deps.broadcast_to_channels.call_args.args[1].type is OutboundEventType.APPROVAL


@pytest.mark.asyncio
async def test_degraded_cop_blocks_fire_and_forget(mock_deps):
    """Fire-and-forget actions fail closed when Cop cannot inspect them."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            new_callable=AsyncMock,
            return_value=CopVerdict(
                flagged=False,
                reason="No gateway available",
                degraded=True,
            ),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ),
    ):
        allowed = await cop_gate(
            "register_group",
            "name=test",
            {"type": "register_group"},
            "admin-1",
            mock_deps,
        )

    assert allowed is False
    event = mock_deps.broadcast_to_channels.call_args.args[1]
    assert event.type is OutboundEventType.SYSTEM
