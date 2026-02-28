"""Tests for the IPC service request handler with trust-based policy enforcement."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.config.models import WorkspaceConfig
from pynchy.db import _init_test_database
from pynchy.ipc._handlers_service import (
    _handle_service_request,
    clear_plugin_handler_cache,
)
from pynchy.security.gate import _gates, create_gate
from pynchy.types import ServiceTrustConfig, WorkspaceProfile, WorkspaceSecurity


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()
    clear_plugin_handler_cache()
    yield
    _gates.clear()


@pytest.fixture
def register_gate():
    """Register a SecurityGate for a test workspace.

    Returns a factory that creates and registers a gate with the given
    service trust configs. The gate is cleaned up after each test by
    the autouse _setup fixture which clears _gates.
    """

    def _make(source_group: str = "test-ws", **service_overrides: ServiceTrustConfig):
        security = WorkspaceSecurity(services=dict(service_overrides))
        return create_gate(source_group, 1000.0, security)

    return _make


class FakeDeps:
    """Minimal IpcDeps for testing."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    async def broadcast_to_channels(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))


TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test",
    folder="test-ws",
    trigger="@Pynchy",
    added_at="2024-01-01",
)


def _make_request(tool_name: str, request_id: str = "test-req-1", **kwargs) -> dict:
    return {
        "type": f"service:{tool_name}",
        "request_id": request_id,
        **kwargs,
    }


def _make_settings(**kwargs):
    """Create a fake Settings with a basic workspace entry.

    Security is now resolved via SecurityGate (registered in tests via
    the register_gate fixture), so this only needs to provide a
    WorkspaceConfig shell for non-security handler logic (cop gate, etc.).
    """

    class FakeSettings:
        def __init__(self):
            self.workspaces = {
                "test-ws": WorkspaceConfig(name="test", **kwargs),
            }
            self.services = {}

    return FakeSettings()


def _make_fake_plugin_manager(*tool_names: str, handler_fn=None):
    """Create a fake plugin manager that provides handlers for the given tool names."""

    async def _stub_handler(data: dict) -> dict:
        return {"error": f"Service '{data.get('type', '')}' is not implemented yet."}

    fn = handler_fn or _stub_handler
    fake_pm = MagicMock()
    fake_pm.hook.pynchy_service_handler.return_value = [
        {"tools": {name: fn for name in tool_names}},
    ]
    return fake_pm


@pytest.mark.asyncio
async def test_plugin_dispatch_calls_handler(tmp_path, register_gate):
    """Test that a plugin-provided handler is called after policy allows."""
    mock_handler = AsyncMock(return_value={"result": {"status": "ok"}})
    fake_pm = _make_fake_plugin_manager("my_tool", handler_fn=mock_handler)

    # Register a gate with all-safe service: no gating
    register_gate(
        my_tool=ServiceTrustConfig(
            public_source=False,
            secret_data=False,
            public_sink=False,
            dangerous_writes=False,
        ),
    )

    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = _make_request("my_tool", some_param="value")
        await _handle_service_request(data, "test-ws", False, deps)

    mock_handler.assert_awaited_once()

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert response == {"result": {"status": "ok"}}


@pytest.mark.asyncio
async def test_forbidden_tool_denied(tmp_path, register_gate):
    """Test that a forbidden tool is denied."""
    fake_pm = _make_fake_plugin_manager("forbidden_tool")

    # Register a gate with forbidden dangerous_writes
    register_gate(
        forbidden_tool=ServiceTrustConfig(dangerous_writes="forbidden"),
    )

    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = _make_request("forbidden_tool", param="value")
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert response_file.exists()
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "Policy denied" in response["error"]


@pytest.mark.asyncio
async def test_dangerous_writes_requires_human(tmp_path, register_gate):
    """Test that dangerous_writes=True triggers human approval gate."""
    fake_pm = _make_fake_plugin_manager("sensitive_tool")

    # Register a gate with dangerous_writes=True
    register_gate(
        sensitive_tool=ServiceTrustConfig(
            public_source=False,
            secret_data=False,
            public_sink=False,
            dangerous_writes=True,
        ),
    )

    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch("pynchy.security.approval.get_settings", return_value=settings),
    ):
        data = _make_request("sensitive_tool", item_id="123")
        await _handle_service_request(data, "test-ws", False, deps)

    # No response file — container blocks until human decides
    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert not response_file.exists()

    # Pending approval file was created
    pending_file = tmp_path / "ipc" / "test-ws" / "pending_approvals" / "test-req-1.json"
    assert pending_file.exists()
    pending = json.loads(pending_file.read_text())
    assert pending["tool_name"] == "sensitive_tool"
    assert pending["request_id"] == "test-req-1"

    # Notification was broadcast
    assert len(deps.broadcast_messages) == 1
    assert "Approval required" in deps.broadcast_messages[0][1]
    assert "sensitive_tool" in deps.broadcast_messages[0][1]


@pytest.mark.asyncio
async def test_unknown_tool_type(tmp_path):
    """Test that unknown tool types get an error response."""
    fake_pm = _make_fake_plugin_manager()  # empty plugin
    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = {
            "type": "service:nonexistent_tool",
            "request_id": "req-unknown",
        }
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "req-unknown.json"
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "Unknown service tool" in response["error"]


@pytest.mark.asyncio
async def test_missing_request_id():
    """Test that missing request_id is handled gracefully."""
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    # Should return without writing any response (just logs warning)
    data = {"type": "service:some_tool"}
    await _handle_service_request(data, "test-ws", False, deps)


@pytest.mark.asyncio
async def test_fallback_security_for_unconfigured_workspace(tmp_path):
    """Workspaces with no gate and no security config get maximally cautious defaults.

    This exercises the ephemeral gate fallback path: no gate registered,
    resolve_security creates a default WorkspaceSecurity, and an ephemeral
    SecurityGate is created for the request.
    """
    fake_pm = _make_fake_plugin_manager("some_tool")

    class FakeSettings:
        def __init__(self):
            self.workspaces = {}  # No workspace configured
            self.services = {}

    settings = FakeSettings()
    settings.data_dir = tmp_path

    deps = FakeDeps({})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.config.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch("pynchy.security.approval.get_settings", return_value=settings),
    ):
        data = _make_request("some_tool")
        await _handle_service_request(data, "unknown-ws", False, deps)

    # Default ServiceTrustConfig has dangerous_writes=True -> needs human
    # No response file written (container blocks)
    response_file = tmp_path / "ipc" / "unknown-ws" / "responses" / "test-req-1.json"
    assert not response_file.exists()

    # Pending approval file created
    pending_file = tmp_path / "ipc" / "unknown-ws" / "pending_approvals" / "test-req-1.json"
    assert pending_file.exists()


@pytest.mark.asyncio
async def test_safe_service_allowed(tmp_path, register_gate):
    """A fully safe service (all False) passes without gating."""
    mock_handler = AsyncMock(return_value={"result": "ok"})
    fake_pm = _make_fake_plugin_manager("safe_tool", handler_fn=mock_handler)

    # Register a gate with all-safe service
    register_gate(
        safe_tool=ServiceTrustConfig(
            public_source=False,
            secret_data=False,
            public_sink=False,
            dangerous_writes=False,
        ),
    )

    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._write.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = _make_request("safe_tool")
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert "result" in response
