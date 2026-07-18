"""Tests for the IPC service request handler with trust-based policy enforcement."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_host_action_catalog

from pynchy import state
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.ipc import registry
from pynchy.host.container_manager.ipc.handlers_service import clear_plugin_handler_cache
from pynchy.host.container_manager.security import gate
from pynchy.host.container_manager.security.gate import create_gate
from pynchy.state import connection
from pynchy.types import ServiceTrustConfig, WorkspaceProfile, WorkspaceSecurity


@pytest.fixture(autouse=True)
async def _setup():
    await state.init_test_database()
    clear_plugin_handler_cache()
    yield
    gate._gates.clear()


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


class FakeDeps(NullIpcDeps):
    """Minimal IpcDeps for testing."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    async def broadcast_to_channels(self, jid: str, event: object) -> None:
        # event is an OutboundEvent; extract .content for string assertions
        content = event.content if hasattr(event, "content") else str(event)
        self.broadcast_messages.append((jid, content))


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
            self.profiles = {"test-profile": ProfileConfig(**kwargs)}
            self.workspaces = {
                "test-ws": WorkspaceConfig(profiles=["test-profile"]),
            }
            self.services = {}
            self.tools = {}

    return FakeSettings()


def _make_action_catalog(*tool_names: str, handler_fn=None, read_tools: tuple[str, ...] = ()):
    """Create a typed catalog for synthetic dispatch tools."""

    async def _stub_handler(data: dict):
        return await asyncio.sleep(
            0,
            result={"error": f"Service '{data.get('type', '')}' is not implemented yet."},
        )

    return make_host_action_catalog(
        *tool_names,
        handler=handler_fn or _stub_handler,
        read_tools=read_tools,
    )


@pytest.mark.asyncio
async def test_plugin_dispatch_calls_handler(tmp_path, register_gate):
    """Test that a plugin-provided handler is called after policy allows."""
    mock_handler = AsyncMock(return_value={"result": {"status": "ok"}})
    catalog = _make_action_catalog("my_tool", handler_fn=mock_handler)

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
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
    ):
        data = _make_request("my_tool", some_param="value")
        await registry.dispatch(data, "test-ws", False, deps)

    mock_handler.assert_awaited_once()

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert response == {"result": {"status": "ok"}}

    db = connection._get_db()
    cursor = await db.execute(
        "SELECT metadata FROM messages WHERE sender = 'security' ORDER BY timestamp"
    )
    events = [json.loads(row["metadata"]) for row in await cursor.fetchall()]
    assert [event["decision"] for event in events] == ["allowed", "execution_succeeded"]
    assert all(event["capability_id"] == "test.my.tool" for event in events)
    assert all(event["action_ids"] == ["test.my.tool"] for event in events)


@pytest.mark.asyncio
async def test_declared_read_tool_taints_untrusted_private_content(tmp_path, register_gate):
    """A Matrix read must taint the turn before later actions can use its text."""
    mock_handler = AsyncMock(return_value={"result": {"messages": ["untrusted text"]}})
    catalog = _make_action_catalog(
        "matrix_list_messages",
        handler_fn=mock_handler,
        read_tools=("matrix_list_messages",),
    )
    registered_gate = register_gate(
        matrix_list_messages=ServiceTrustConfig(
            public_source=True,
            secret_data=True,
            public_sink=False,
            dangerous_writes=False,
        )
    )

    settings = _make_settings()
    settings.data_dir = tmp_path
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
    ):
        await registry.dispatch(_make_request("matrix_list_messages"), "test-ws", False, deps)

    mock_handler.assert_awaited_once()
    assert registered_gate.policy.corruption_tainted is True
    assert registered_gate.policy.secret_tainted is True


@pytest.mark.asyncio
async def test_forbidden_tool_denied(tmp_path, register_gate):
    """Test that a forbidden tool is denied."""
    catalog = _make_action_catalog("forbidden_tool")

    # Register a gate with forbidden dangerous_writes
    register_gate(
        forbidden_tool=ServiceTrustConfig(dangerous_writes="forbidden"),
    )

    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
    ):
        data = _make_request("forbidden_tool", param="value")
        await registry.dispatch(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert response_file.exists()
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "Policy denied" in response["error"]


@pytest.mark.asyncio
async def test_dangerous_writes_requires_human(tmp_path, register_gate):
    """Test that dangerous_writes=True triggers human approval gate."""
    catalog = _make_action_catalog("sensitive_tool")

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
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.security.approval.get_settings", return_value=settings
        ),
    ):
        data = _make_request("sensitive_tool", item_id="123")
        await registry.dispatch(data, "test-ws", False, deps)

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
    catalog = _make_action_catalog(handler_fn=AsyncMock())
    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
    ):
        data = {
            "type": "service:nonexistent_tool",
            "request_id": "req-unknown",
        }
        await registry.dispatch(data, "test-ws", False, deps)

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
    await registry.dispatch(data, "test-ws", False, deps)


@pytest.mark.asyncio
async def test_non_string_request_id_is_ignored():
    """Service IPC payloads must parse request_id as a non-empty string."""
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    data = {"type": "service:some_tool", "request_id": 123}
    await registry.dispatch(data, "test-ws", False, deps)


@pytest.mark.asyncio
async def test_fallback_security_for_unconfigured_workspace(tmp_path):
    """Workspaces with no gate and no security config get maximally cautious defaults.

    This exercises the ephemeral gate fallback path: no gate registered,
    resolve_security creates a default WorkspaceSecurity, and an ephemeral
    SecurityGate is created for the request.
    """
    catalog = _make_action_catalog("some_tool")

    class FakeSettings:
        def __init__(self):
            self.workspaces = {}  # No workspace configured
            self.services = {}

    settings = FakeSettings()
    settings.data_dir = tmp_path

    deps = FakeDeps({})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.config.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.security.approval.get_settings", return_value=settings
        ),
    ):
        data = _make_request("some_tool")
        await registry.dispatch(data, "unknown-ws", False, deps)

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
    catalog = _make_action_catalog("safe_tool", handler_fn=mock_handler)

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
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
    ):
        data = _make_request("safe_tool")
        await registry.dispatch(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert "result" in response
