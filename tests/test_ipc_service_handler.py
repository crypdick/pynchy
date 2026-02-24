"""Tests for the IPC service request handler with trust-based policy enforcement."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.config_models import (
    ServiceTrustTomlConfig,
    WorkspaceConfig,
    WorkspaceSecurityTomlConfig,
)
from pynchy.db import _init_test_database
from pynchy.ipc._handlers_service import (
    _handle_service_request,
    clear_plugin_handler_cache,
)
from pynchy.types import WorkspaceProfile


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()
    clear_plugin_handler_cache()


class FakeDeps:
    """Minimal IpcDeps for testing."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups


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


def _make_settings(ws_security: WorkspaceSecurityTomlConfig | None = None, **kwargs):
    """Create a fake Settings with workspace security configured."""

    class FakeSettings:
        def __init__(self):
            self.workspaces = {
                "test-ws": WorkspaceConfig(name="test", security=ws_security, **kwargs),
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
async def test_plugin_dispatch_calls_handler(tmp_path):
    """Test that a plugin-provided handler is called after policy allows."""
    mock_handler = AsyncMock(return_value={"result": {"status": "ok"}})
    fake_pm = _make_fake_plugin_manager("my_tool", handler_fn=mock_handler)

    # All-safe service: no gating
    settings = _make_settings(
        ws_security=WorkspaceSecurityTomlConfig(
            services={
                "my_tool": ServiceTrustTomlConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            },
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = _make_request("my_tool", some_param="value")
        await _handle_service_request(data, "test-ws", False, deps)

    mock_handler.assert_awaited_once()

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert response == {"result": {"status": "ok"}}


@pytest.mark.asyncio
async def test_forbidden_tool_denied(tmp_path):
    """Test that a forbidden tool is denied."""
    fake_pm = _make_fake_plugin_manager("forbidden_tool")
    settings = _make_settings(
        ws_security=WorkspaceSecurityTomlConfig(
            services={
                "forbidden_tool": ServiceTrustTomlConfig(
                    dangerous_writes="forbidden",
                ),
            },
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
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
async def test_dangerous_writes_requires_human(tmp_path):
    """Test that dangerous_writes=True triggers human approval gate."""
    fake_pm = _make_fake_plugin_manager("sensitive_tool")
    settings = _make_settings(
        ws_security=WorkspaceSecurityTomlConfig(
            services={
                "sensitive_tool": ServiceTrustTomlConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=True,
                ),
            },
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = _make_request("sensitive_tool", item_id="123")
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "approval" in response["error"].lower()


@pytest.mark.asyncio
async def test_unknown_tool_type(tmp_path):
    """Test that unknown tool types get an error response."""
    fake_pm = _make_fake_plugin_manager()  # empty plugin
    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
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
    """Workspaces with no security config get maximally cautious defaults."""
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
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = _make_request("some_tool")
        await _handle_service_request(data, "unknown-ws", False, deps)

    # Default ServiceTrustConfig has dangerous_writes=True -> needs human
    response_file = tmp_path / "ipc" / "unknown-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "approval" in response["error"].lower()


@pytest.mark.asyncio
async def test_safe_service_allowed(tmp_path):
    """A fully safe service (all False) passes without gating."""
    mock_handler = AsyncMock(return_value={"result": "ok"})
    fake_pm = _make_fake_plugin_manager("safe_tool", handler_fn=mock_handler)
    settings = _make_settings(
        ws_security=WorkspaceSecurityTomlConfig(
            services={
                "safe_tool": ServiceTrustTomlConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            },
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
    ):
        data = _make_request("safe_tool")
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert "result" in response
