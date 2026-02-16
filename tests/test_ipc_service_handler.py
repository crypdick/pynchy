"""Tests for the IPC service request handler with policy enforcement."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pynchy.config import (
    McpToolSecurityConfig,
    RateLimitsConfig,
    WorkspaceConfig,
    WorkspaceSecurityConfig,
)
from pynchy.db import _init_test_database
from pynchy.ipc._handlers_service import (
    SERVICE_TOOL_TYPES,
    _handle_service_request,
    clear_policy_cache,
)
from pynchy.types import RegisteredGroup


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()
    clear_policy_cache()


class FakeDeps:
    """Minimal IpcDeps for testing."""

    def __init__(self, groups: dict[str, RegisteredGroup] | None = None):
        self._groups = groups or {}

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        return self._groups


TEST_GROUP = RegisteredGroup(
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


def _make_settings(ws_security: WorkspaceSecurityConfig | None = None, **kwargs):
    """Create a fake Settings with workspace security configured."""

    class FakeSettings:
        def __init__(self):
            self.workspaces = {
                "test-ws": WorkspaceConfig(security=ws_security, **kwargs),
            }

    return FakeSettings()


@pytest.mark.asyncio
async def test_allowed_always_approve_tool(tmp_path):
    """Test that an always-approve tool gets through and returns not-implemented."""
    settings = _make_settings(
        ws_security=WorkspaceSecurityConfig(
            mcp_tools={"read_email": McpToolSecurityConfig(risk_tier="always-approve")},
            default_risk_tier="human-approval",
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with patch("pynchy.ipc._handlers_service.get_settings", return_value=settings):
        data = _make_request("read_email", folder="INBOX", limit=10)
        await _handle_service_request(data, "test-ws", False, deps)

    # Check response was written (should be "not implemented" since no backend)
    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert response_file.exists()
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "not implemented" in response["error"].lower()


@pytest.mark.asyncio
async def test_denied_disabled_tool(tmp_path):
    """Test that a disabled tool is denied."""
    settings = _make_settings(
        ws_security=WorkspaceSecurityConfig(
            mcp_tools={
                "send_email": McpToolSecurityConfig(risk_tier="human-approval", enabled=False)
            },
            default_risk_tier="human-approval",
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with patch("pynchy.ipc._handlers_service.get_settings", return_value=settings):
        data = _make_request("send_email", to="a@b.com", subject="hi", body="test")
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert response_file.exists()
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "Policy denied" in response["error"]


@pytest.mark.asyncio
async def test_human_approval_required(tmp_path):
    """Test that human-approval tier returns approval-required error."""
    settings = _make_settings(
        ws_security=WorkspaceSecurityConfig(
            mcp_tools={
                "get_password": McpToolSecurityConfig(risk_tier="human-approval", enabled=True)
            },
            default_risk_tier="human-approval",
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with patch("pynchy.ipc._handlers_service.get_settings", return_value=settings):
        data = _make_request("get_password", item_id="123")
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "approval" in response["error"].lower()


@pytest.mark.asyncio
async def test_rate_limited(tmp_path):
    """Test that rate-limited calls are denied."""
    settings = _make_settings(
        ws_security=WorkspaceSecurityConfig(
            mcp_tools={"read_email": McpToolSecurityConfig(risk_tier="always-approve")},
            default_risk_tier="human-approval",
            rate_limits=RateLimitsConfig(max_calls_per_hour=1),
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with patch("pynchy.ipc._handlers_service.get_settings", return_value=settings):
        # First call succeeds
        data = _make_request("read_email", request_id="req-1")
        await _handle_service_request(data, "test-ws", False, deps)

        response_file = tmp_path / "ipc" / "test-ws" / "responses" / "req-1.json"
        response = json.loads(response_file.read_text())
        assert "not implemented" in response["error"].lower()  # allowed but not wired

        # Second call is rate-limited
        data = _make_request("read_email", request_id="req-2")
        await _handle_service_request(data, "test-ws", False, deps)

        response_file = tmp_path / "ipc" / "test-ws" / "responses" / "req-2.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "rate limit" in response["error"].lower()


@pytest.mark.asyncio
async def test_unknown_tool_type(tmp_path):
    """Test that unknown tool types get an error response."""
    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with patch("pynchy.ipc._handlers_service.get_settings", return_value=settings):
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
    data = {"type": "service:read_email"}
    await _handle_service_request(data, "test-ws", False, deps)


@pytest.mark.asyncio
async def test_fallback_security_for_unconfigured_workspace(tmp_path):
    """Test that workspaces with no security config get strict defaults."""

    class FakeSettings:
        def __init__(self):
            self.workspaces = {}  # No workspace configured

    settings = FakeSettings()
    settings.data_dir = tmp_path

    deps = FakeDeps({})

    with patch("pynchy.ipc._handlers_service.get_settings", return_value=settings):
        data = _make_request("read_email")
        await _handle_service_request(data, "unknown-ws", False, deps)

    # Default WorkspaceSecurity has default_risk_tier="human-approval"
    response_file = tmp_path / "ipc" / "unknown-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "approval" in response["error"].lower()


@pytest.mark.asyncio
async def test_unconfigured_tool_uses_default_tier(tmp_path):
    """Test that tools not listed in mcp_tools use default_risk_tier."""
    settings = _make_settings(
        ws_security=WorkspaceSecurityConfig(
            mcp_tools={},  # No tools configured
            default_risk_tier="always-approve",
        ),
    )
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with patch("pynchy.ipc._handlers_service.get_settings", return_value=settings):
        data = _make_request("read_email")
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    # Should be approved (always-approve default) but return not-implemented
    assert "not implemented" in response["error"].lower()


def test_service_tool_types_complete():
    """Verify all expected service tool types are registered."""
    expected = {
        "read_email",
        "send_email",
        "list_calendar",
        "create_event",
        "delete_event",
        "search_passwords",
        "get_password",
    }
    assert expected == SERVICE_TOOL_TYPES
