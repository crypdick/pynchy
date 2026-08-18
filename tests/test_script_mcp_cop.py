"""Tests for host-process MCP auto-classification as host-mutating.

Script and stdio MCP servers run as host subprocesses, so any tool call
targeting them is implicitly host-mutating and must go through the Cop gate.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import NullIpcDeps, make_host_action_catalog

from pynchy.config.api import McpTool, McpToolConfig
from pynchy.host.container_manager.ipc.handlers_service import clear_plugin_handler_cache
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate
from pynchy.state import init_test_database
from pynchy.workspace.api import (
    CapabilityRule,
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)


@pytest.fixture(autouse=True)
async def _setup():
    await init_test_database()
    clear_plugin_handler_cache()
    yield
    destroy_gate("test-ws", 1000.0)


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


class FakeDeps(NullIpcDeps):
    """Minimal IpcDeps for testing."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    async def broadcast_to_channels(self, jid: str, event: object) -> None:
        content = event.content if hasattr(event, "content") else str(event)
        self.broadcast_messages.append((jid, content))


def _make_request(tool_name: str, request_id: str = "req-1", **extra) -> dict:
    return {"type": f"service:{tool_name}", "request_id": request_id, **extra}


def _register_safe_gate(tool_name: str) -> None:
    """Register a SecurityGate with all-safe trust for a tool."""
    security = WorkspaceSecurity(
        capabilities={"*": CapabilityRule("allow")},
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
    mcp_type: Literal["script", "stdio", "docker", "url"] = "script",
    *,
    tmp_path=None,
) -> MagicMock:
    """Create fake Settings with a typed MCP tool entry.

    Security is now resolved via SecurityGate (registered separately),
    so this only needs to provide tool provider config for the cop gate check.
    """
    mock_s = MagicMock()
    mcp_config = {
        "script": {"runtime": "script", "command": "uv", "port": 8474},
        "stdio": {
            "runtime": "stdio",
            "command": "npx",
            "port": 8474,
            "transport": "streamable_http",
        },
        "docker": {"runtime": "docker", "image": "mcp/example:latest", "port": 8080},
        "url": {"runtime": "url", "url": "https://example.com/mcp"},
    }[mcp_type]
    mock_s.tools = {
        tool_name: McpTool(type="mcp", mcp=McpToolConfig(**mcp_config)),
    }
    mock_s.workspaces = {}
    if tmp_path is not None:
        mock_s.data_dir = tmp_path
    return mock_s


def _make_action_catalog(*tool_names: str, handler_fn=None):
    """Create a typed catalog for synthetic script-MCP tools."""

    async def _stub_handler(_data: dict):
        return await asyncio.sleep(0, result={"result": "ok"})

    return make_host_action_catalog(*tool_names, handler=handler_fn or _stub_handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_type", ["script", "stdio"])
async def test_host_mcp_triggers_cop_gate(tmp_path, mcp_type):
    """A service request targeting a host-process MCP should invoke cop_gate."""
    tool = "my_script"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "ok"})
    catalog = _make_action_catalog(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, mcp_type, tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_cop,
    ):
        data = _make_request(tool, some_param="value")
        await dispatch(data, "test-ws", False, deps)

    mock_cop.assert_called_once()
    # Verify operation name follows the "script_mcp:<tool>" convention
    assert mock_cop.call_args.args[0] == f"script_mcp:{tool}"


@pytest.mark.asyncio
async def test_non_script_mcp_skips_cop_gate(tmp_path):
    """A docker-type MCP should NOT trigger cop_gate."""
    tool = "my_docker"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "ok"})
    catalog = _make_action_catalog(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "docker", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_cop,
    ):
        data = _make_request(tool)
        await dispatch(data, "test-ws", False, deps)

    mock_cop.assert_not_called()
    # Handler should still have been called (no cop gate blocking)
    mock_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_script_mcp_blocked_by_cop(tmp_path):
    """When cop_gate returns False for a script MCP, the handler is NOT called."""
    tool = "my_script"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "ok"})
    catalog = _make_action_catalog(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "script", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        data = _make_request(tool, some_param="value")
        await dispatch(data, "test-ws", False, deps)

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
    catalog = _make_action_catalog(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "script", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        data = _make_request(tool, some_param="value")
        await dispatch(data, "test-ws", False, deps)

    mock_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_caller_asserted_cop_approval_is_ignored(tmp_path):
    """A propagated boolean cannot bypass Cop for a script MCP."""
    tool = "my_script"
    _register_safe_gate(tool)
    mock_handler = AsyncMock(return_value={"result": "ok"})
    catalog = _make_action_catalog(tool, handler_fn=mock_handler)
    settings = _make_settings_with_mcp(tool, "script", tmp_path=tmp_path)
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_cop,
    ):
        data = _make_request(tool, _cop_approved=True, some_param="value")
        await dispatch(data, "test-ws", False, deps)

    mock_cop.assert_called_once()
    # But the handler should still run
    mock_handler.assert_awaited_once()
