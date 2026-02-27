# tests/test_script_mcp_cop.py
"""Tests for script-type MCP auto-classification as host-mutating.

Script-type MCP servers run as host subprocesses, so any tool call targeting
them is implicitly host-mutating and must go through the Cop gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.db import _init_test_database
from pynchy.ipc._handlers_service import (
    _handle_service_request,
    clear_plugin_handler_cache,
)
from pynchy.security.gate import _gates, create_gate
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity, WorkspaceProfile


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()
    clear_plugin_handler_cache()
    yield
    _gates.clear()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test",
    folder="test-ws",
    trigger="@Pynchy",
    added_at="2024-01-01",
)


class FakeDeps:
    """Minimal IpcDeps for testing."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    async def broadcast_to_channels(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))


def _make_request(tool_name: str, request_id: str = "req-1", **extra) -> dict:
    return {"type": f"service:{tool_name}", "request_id": request_id, **extra}


def _register_safe_gate(tool_name: str) -> None:
    """Register a SecurityGate with all-safe trust for a tool."""
    security = WorkspaceSecurity(
        services={
            tool_name: ServiceTrustConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            ),
        },
    )
    create_gate("test-ws", 1000.0, security)


def _make_settings_with_mcp(
    tool_name: str,
    mcp_type: str = "script",
    *,
    tmp_path=None,
) -> MagicMock:
    """Create fake Settings with an MCP server entry.

    Security is now resolved via SecurityGate (registered separately),
    so this only needs to provide mcp_servers config for the cop gate check.
    """
    mock_s = MagicMock()
    mcp_mock = MagicMock()
    mcp_mock.type = mcp_type
    mock_s.mcp_servers = {tool_name: mcp_mock}
    mock_s.services = {}
    mock_s.workspaces = {}
    if tmp_path is not None:
        mock_s.data_dir = tmp_path
    return mock_s


def _make_fake_plugin_manager(*tool_names: str, handler_fn=None):
    """Create a fake plugin manager that provides handlers for the given tools."""

    async def _stub_handler(data: dict) -> dict:
        return {"result": "ok"}

    fn = handler_fn or _stub_handler
    fake_pm = MagicMock()
    fake_pm.hook.pynchy_service_handler.return_value = [
        {"tools": {name: fn for name in tool_names}},
    ]
    return fake_pm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_script_mcp_triggers_cop_gate(tmp_path):
    """A service request targeting a script-type MCP should invoke cop_gate."""
    tool = "my_script"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "ok"})
    fake_pm = _make_fake_plugin_manager(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "script", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_cop,
    ):
        data = _make_request(tool, some_param="value")
        await _handle_service_request(data, "test-ws", False, deps)

    mock_cop.assert_called_once()
    # Verify operation name follows the "script_mcp:<tool>" convention
    assert mock_cop.call_args.args[0] == f"script_mcp:{tool}"


@pytest.mark.asyncio
async def test_non_script_mcp_skips_cop_gate(tmp_path):
    """A docker-type MCP should NOT trigger cop_gate."""
    tool = "my_docker"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "ok"})
    fake_pm = _make_fake_plugin_manager(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "docker", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_cop,
    ):
        data = _make_request(tool)
        await _handle_service_request(data, "test-ws", False, deps)

    mock_cop.assert_not_called()
    # Handler should still have been called (no cop gate blocking)
    mock_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_script_mcp_blocked_by_cop(tmp_path):
    """When cop_gate returns False for a script MCP, the handler is NOT called."""
    tool = "my_script"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "ok"})
    fake_pm = _make_fake_plugin_manager(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "script", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        data = _make_request(tool, some_param="value")
        await _handle_service_request(data, "test-ws", False, deps)

    # Handler must NOT be called when cop blocks
    mock_handler.assert_not_awaited()
    # No response file should be written (cop_gate handles the escalation)
    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "req-1.json"
    assert not response_file.exists()


@pytest.mark.asyncio
async def test_script_mcp_allowed_by_cop(tmp_path):
    """When cop_gate returns True for a script MCP, the handler IS called."""
    tool = "my_script"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "dispatched"})
    fake_pm = _make_fake_plugin_manager(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "script", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        data = _make_request(tool, some_param="value")
        await _handle_service_request(data, "test-ws", False, deps)

    mock_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_cop_approved_skips_gate(tmp_path):
    """When _cop_approved=True, cop_gate is NOT called even for script MCP."""
    tool = "my_script"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "ok"})
    fake_pm = _make_fake_plugin_manager(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "script", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_cop,
    ):
        data = _make_request(tool, _cop_approved=True, some_param="value")
        await _handle_service_request(data, "test-ws", False, deps)

    # cop_gate must NOT be called when _cop_approved is set
    mock_cop.assert_not_called()
    # But the handler should still run
    mock_handler.assert_awaited_once()
