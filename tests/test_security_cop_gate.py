"""Tests for cop_gate host-mutating operation gating."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.container_manager.security.cop import CopVerdict


@pytest.fixture
def mock_deps():
    deps = MagicMock()
    deps.workspaces.return_value = {"jid-1": MagicMock(folder="admin-1")}
    deps.broadcast_to_channels = AsyncMock()
    deps.broadcast_host_message = AsyncMock()
    return deps


@pytest.mark.asyncio
async def test_cop_allows_clean_operation(mock_deps):
    """Clean operation passes through — cop_gate returns True."""
    from pynchy.host.container_manager.security.cop_gate import cop_gate

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
async def test_cop_blocks_flagged_with_request_id(mock_deps):
    """Flagged operation with request_id creates pending approval and returns False."""
    from pynchy.host.container_manager.security.cop_gate import cop_gate

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
            "pynchy.host.container_manager.security.cop_gate.create_pending_approval"
        ) as mock_create,
        patch(
            "pynchy.host.container_manager.security.cop_gate.format_approval_notification",
            return_value="msg",
        ),
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


@pytest.mark.asyncio
async def test_cop_blocks_flagged_fire_and_forget(mock_deps):
    """Flagged fire-and-forget operation broadcasts warning, no approval."""
    from pynchy.host.container_manager.security.cop_gate import cop_gate

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
    from pynchy.host.container_manager.security.cop_gate import cop_gate

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
    from pynchy.host.container_manager.security.cop_gate import cop_gate

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
    from pynchy.host.container_manager.security.cop_gate import cop_gate

    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            return_value=CopVerdict(flagged=True, reason="backdoor pattern detected"),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.record_security_event",
            new_callable=AsyncMock,
        ),
        patch("pynchy.host.container_manager.security.cop_gate.create_pending_approval"),
        patch(
            "pynchy.host.container_manager.security.cop_gate.format_approval_notification",
            return_value="approval msg",
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
    notification_text = broadcast_call.args[1]
    assert "backdoor pattern detected" in notification_text
