"""Tests for the IPC service request handler with trust-based policy enforcement."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_host_action_catalog, make_settings

import pynchy.host.container_manager.ipc.registry as registry
from pynchy import state
from pynchy.config.api import LinearTool, ProfileConfig, ResolvedWorkspaceConfig, WorkspaceConfig
from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalDecisionContext,
    ApprovalReplayPolicy,
    approval_replay_validation_error,
)
from pynchy.host.container_manager.ipc.handlers_service import clear_plugin_handler_cache
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    build_workspace_security,
    create_gate,
    destroy_gate,
)
from pynchy.plugins.api import ApprovalMode, HostActionCatalog
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
from pynchy.workspace.api import (
    CapabilityRule,
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)


@pytest.fixture(autouse=True)
async def _setup(monkeypatch: pytest.MonkeyPatch):
    await state.init_test_database()
    clear_plugin_handler_cache()
    created: list[tuple[str, float]] = []
    original_create_gate = create_gate

    def track_created_gate(
        source_group: str,
        invocation_ts: float,
        security: WorkspaceSecurity,
    ):
        created.append((source_group, invocation_ts))
        return original_create_gate(source_group, invocation_ts, security)

    monkeypatch.setitem(globals(), "create_gate", track_created_gate)
    yield
    for source_group, invocation_ts in created:
        destroy_gate(source_group, invocation_ts)


@pytest.fixture
def register_gate():
    """Register a SecurityGate for a test workspace.

    Returns a factory that creates and registers a gate with the given
    service trust configs. The gate is cleaned up after each test by
    the autouse _setup fixture which clears _gates.
    """

    def _make(source_group: str = "test-ws", **service_overrides: ServiceTrustConfig):
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}
            if all(trust.dangerous_writes is False for trust in service_overrides.values())
            else {},
            services=dict(service_overrides),
        )
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


def _make_action_catalog(
    *tool_names: str,
    handler_fn=None,
    read_tools: tuple[str, ...] = (),
    approval_mode: ApprovalMode = ApprovalMode.EXACT_REQUEST,
):
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
        approval_mode=approval_mode,
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
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
    ):
        data = _make_request("my_tool", some_param="value", source_group="attacker-workspace")
        await registry.dispatch(data, "test-ws", False, deps)

    mock_handler.assert_awaited_once_with(
        {
            "type": "service:my_tool",
            "request_id": "test-req-1",
            "some_param": "value",
            "source_group": "test-ws",
        }
    )

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    response = json.loads(response_file.read_text())
    assert response == {"result": {"status": "ok"}}

    history = await state.get_chat_history("test@g.us", limit=10)
    events = [message.metadata for message in history if message.sender == "security"]
    assert [event["decision"] for event in events] == ["allowed", "execution_succeeded"]
    assert all(event["capability_id"] == "test.my.tool" for event in events)
    assert all(event["action_ids"] == ["test.my.tool"] for event in events)


@pytest.mark.asyncio
async def test_named_linear_account_admits_stable_host_service_action(tmp_path):
    """A named Linear credential enables host actions requiring the `linear` alias."""
    mock_handler = AsyncMock(return_value={"result": {"work_items": []}})
    registered_action = next(
        action
        for action in host_action_registration().actions
        if action.tool_name == "linear_list_work_items"
    )
    action = replace(registered_action, handler=mock_handler)
    catalog = HostActionCatalog(actions=(action,))
    settings = make_settings(
        data_dir=tmp_path,
        profiles={
            "synapse": ProfileConfig(
                tools=["linear_synapse"],
                permissions={"allow": ["linear.workitem.list"]},
            )
        },
        workspaces={"test-ws": WorkspaceConfig(profiles=["synapse"])},
        tools={
            "linear_synapse": LinearTool(
                type="linear",
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            )
        },
    )
    resolved = settings.resolved_workspace_config("test-ws")
    assert resolved is not None
    create_gate("test-ws", 1000.0, build_workspace_security(settings, resolved))
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
    ):
        await registry.dispatch(_make_request("linear_list_work_items"), "test-ws", False, deps)

    mock_handler.assert_awaited_once_with(
        {
            "type": "service:linear_list_work_items",
            "request_id": "test-req-1",
            "source_group": "test-ws",
        }
    )
    response = json.loads((tmp_path / "ipc/test-ws/responses/test-req-1.json").read_text())
    assert response == {"result": {"work_items": []}}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["matrix_route_read", "matrix_route_send"])
async def test_raw_ipc_cannot_invoke_route_tool_omitted_by_runtime_policy(
    tmp_path,
    tool_name,
):
    mock_handler = AsyncMock(return_value={"result": "unsafe"})
    catalog = _make_action_catalog(
        "matrix_route_read",
        "matrix_route_send",
        handler_fn=mock_handler,
    )
    settings = _make_settings()
    settings.data_dir = tmp_path
    resolved = ResolvedWorkspaceConfig(
        skills=[],
        tools=[],
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
    )

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_active_matrix_route",
            return_value=object(),
        ),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
    ):
        await registry.dispatch(_make_request(tool_name), "routed-workspace", False, FakeDeps())

    mock_handler.assert_not_awaited()
    response_file = tmp_path / "ipc/routed-workspace/responses/test-req-1.json"
    response = json.loads(response_file.read_text())
    assert response == {"error": f"Service tool is not enabled for this route: {tool_name}"}


@pytest.mark.asyncio
async def test_declared_read_tool_taints_untrusted_private_content(tmp_path, register_gate):
    """A Matrix read must taint the turn before later actions can use its text."""
    mock_handler = AsyncMock(return_value={"result": {"messages": ["untrusted text"]}})
    catalog = _make_action_catalog(
        "matrix_route_read",
        handler_fn=mock_handler,
        read_tools=("matrix_route_read",),
    )
    registered_gate = register_gate(
        matrix_route_read=ServiceTrustConfig(
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
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
    ):
        await registry.dispatch(_make_request("matrix_route_read"), "test-ws", False, deps)

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

    settings = make_settings(data_dir=tmp_path)

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
    ):
        data = _make_request("forbidden_tool", param="value")
        await registry.dispatch(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert response_file.exists()
    response = json.loads(response_file.read_text())
    assert "error" in response
    assert "Policy denied" in response["error"]


@pytest.mark.parametrize(
    ("approval_mode", "has_session_notice"),
    [
        (ApprovalMode.EXACT_REQUEST, False),
        (ApprovalMode.SESSION_TOOL, False),
    ],
)
@pytest.mark.asyncio
async def test_dangerous_writes_requires_human(
    tmp_path,
    register_gate,
    approval_mode: ApprovalMode,
    *,
    has_session_notice: bool,
):
    """Test that dangerous_writes=True triggers human approval gate."""
    catalog = _make_action_catalog("sensitive_tool", approval_mode=approval_mode)

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
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ),
    ):
        data = _make_request("sensitive_tool", item_id="123")
        await registry.dispatch(data, "test-ws", False, deps)

    # No response file — container blocks until human decides
    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert not response_file.exists()

    # Pending approval file was created
    pending_file = tmp_path / "approvals" / "test-ws" / "pending_approvals" / "test-req-1.json"
    assert pending_file.exists()
    pending = json.loads(pending_file.read_text())
    assert pending["tool_name"] == "sensitive_tool"
    assert pending["request_id"] == "test-req-1"

    # Notification was broadcast
    assert len(deps.broadcast_messages) == 1
    assert "Approval required" in deps.broadcast_messages[0][1]
    assert "sensitive_tool" in deps.broadcast_messages[0][1]
    assert (
        "Approving grants this tool for the rest of the active agent session"
        in deps.broadcast_messages[0][1]
    ) is has_session_notice
    assert "approve-session" in deps.broadcast_messages[0][1]
    assert "approve-forever" in deps.broadcast_messages[0][1]


@pytest.mark.asyncio
async def test_omitted_workspace_tool_fails_before_human_approval(tmp_path, register_gate):
    """A stable IPC schema cannot authorize a tool omitted by the active profile."""
    mock_handler = AsyncMock(return_value={"result": "unsafe"})
    catalog = _make_action_catalog("computer_use", handler_fn=mock_handler)
    register_gate(
        computer_use=ServiceTrustConfig(
            public_source=False,
            secret_data=False,
            public_sink=False,
            dangerous_writes=True,
        )
    )
    settings = _make_settings()
    settings.data_dir = tmp_path
    resolved = ResolvedWorkspaceConfig(
        skills=[],
        tools=[],
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
    )
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
    ):
        await registry.dispatch(_make_request("computer_use"), "test-ws", False, deps)

    response_file = tmp_path / "ipc/test-ws/responses/test-req-1.json"
    assert json.loads(response_file.read_text()) == {
        "error": "Host capability unavailable: Tool computer_use is not enabled for this workspace"
    }
    assert not (tmp_path / "approvals/test-ws/pending_approvals").exists()
    assert deps.broadcast_messages == []
    mock_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_replay_rejects_tool_removed_from_workspace_profile():
    """An old approval cannot revive a host capability removed before replay."""
    action = _make_action_catalog("computer_use").action_for("computer_use")
    assert action is not None
    resolved = ResolvedWorkspaceConfig(
        skills=[],
        tools=[],
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
    )
    context = ApprovalDecisionContext(
        request_id="approval-1",
        source_group="test-ws",
        tool_name="computer_use",
        chat_jid="test@g.us",
        request_data={"action": "list_apps"},
        approved=True,
        approver="operator",
        approved_at="2026-07-22T00:00:00+00:00",
        handler_type="service",
        action=action,
        gate=SecurityGate(WorkspaceSecurity()),
        capability_id="test.computer.use",
        action_ids=("test.computer.use",),
        origin_conversation_id=None,
        action_payload=None,
        action_payload_sha256=None,
        requested_at=None,
        expires_after_seconds=300,
    )

    error = await approval_replay_validation_error(
        context,
        NullIpcDeps(),
        ApprovalReplayPolicy(
            configured_security=lambda _group: WorkspaceSecurity(),
            workspace_tools=lambda _group: tuple(resolved.tools),
        ),
    )

    assert error == "host tool computer_use is no longer enabled for this workspace"


@pytest.mark.asyncio
async def test_explicit_profile_allow_executes_dangerous_write_without_approval(tmp_path):
    """An explicit capability allow is authoritative over the routine write gate."""
    mock_handler = AsyncMock(return_value={"result": "ok"})
    catalog = _make_action_catalog("sensitive_tool", handler_fn=mock_handler)
    create_gate(
        "test-ws",
        1000.0,
        WorkspaceSecurity(
            services={
                "sensitive_tool": ServiceTrustConfig(dangerous_writes=True),
            },
            capabilities={
                "test.sensitive.tool": CapabilityRule(decision="allow"),
            },
        ),
    )
    settings = _make_settings()
    settings.data_dir = tmp_path
    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
    ):
        await registry.dispatch(_make_request("sensitive_tool"), "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "test-req-1.json"
    assert json.loads(response_file.read_text()) == {"result": "ok"}
    assert not (tmp_path / "approvals" / "test-ws" / "pending_approvals").exists()
    assert deps.broadcast_messages == []
    mock_handler.assert_awaited_once()


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
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
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

        def workspace_config(self, workspace_name):
            return self.workspaces.get(workspace_name)

    settings = FakeSettings()
    settings.data_dir = tmp_path

    deps = FakeDeps({})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.config.api.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=catalog,
        ),
        patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ),
    ):
        data = _make_request("some_tool")
        await registry.dispatch(data, "unknown-ws", False, deps)

    # Default ServiceTrustConfig has dangerous_writes=True -> needs human
    # No response file written (container blocks)
    response_file = tmp_path / "ipc" / "unknown-ws" / "responses" / "test-req-1.json"
    assert not response_file.exists()

    # Pending approval file created
    pending_file = tmp_path / "approvals" / "unknown-ws" / "pending_approvals" / "test-req-1.json"
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
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
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
