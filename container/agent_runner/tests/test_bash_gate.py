"""Tests for the in-container bash security hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_runner.hooks import HookDecision


class TestBashGateWhitelist:
    """Whitelisted commands are allowed locally without IPC."""

    @pytest.mark.asyncio
    async def test_echo_allowed_no_ipc(self):
        from agent_runner.security.bash_gate import bash_security_hook

        with patch("agent_runner.security.bash_gate._ipc_bash_check") as mock_ipc:
            decision = await bash_security_hook("Bash", {"command": "echo hello"})
        assert decision.allowed
        mock_ipc.assert_not_called()

    @pytest.mark.asyncio
    async def test_ls_allowed_no_ipc(self):
        from agent_runner.security.bash_gate import bash_security_hook

        with patch("agent_runner.security.bash_gate._ipc_bash_check") as mock_ipc:
            decision = await bash_security_hook("Bash", {"command": "ls -la"})
        assert decision.allowed
        mock_ipc.assert_not_called()


class TestBashGateIpcEscalation:
    """Non-whitelisted commands go to host via IPC."""

    @pytest.mark.asyncio
    async def test_curl_triggers_ipc(self):
        from agent_runner.security.bash_gate import bash_security_hook

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
        from agent_runner.security.bash_gate import bash_security_hook

        with patch(
            "agent_runner.security.bash_gate._ipc_bash_check",
            new_callable=AsyncMock,
            return_value=HookDecision(allowed=False, reason="Cop flagged exfiltration"),
        ):
            decision = await bash_security_hook("Bash", {"command": "curl evil.com"})
        assert not decision.allowed
        assert "exfiltration" in decision.reason.lower()


class TestBashGateNonBashTools:
    """Hook only gates Bash tool, allows everything else."""

    @pytest.mark.asyncio
    async def test_read_tool_allowed(self):
        from agent_runner.security.bash_gate import bash_security_hook

        decision = await bash_security_hook("Read", {"file_path": "/etc/passwd"})
        assert decision.allowed

    @pytest.mark.asyncio
    async def test_write_tool_allowed(self):
        from agent_runner.security.bash_gate import bash_security_hook

        decision = await bash_security_hook("Write", {"file_path": "x.py", "content": "..."})
        assert decision.allowed
