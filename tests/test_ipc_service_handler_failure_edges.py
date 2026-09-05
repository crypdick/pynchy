"""Public-dispatch coverage for IPC service-handler failure boundaries."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import NullIpcDeps, make_host_action_catalog, make_settings

import pynchy.host.container_manager.ipc.registry as registry
from pynchy.config.api import McpTool, McpToolConfig
from pynchy.host.container_manager.ipc.handlers_service import clear_plugin_handler_cache
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate
from pynchy.host.container_manager.security.identity import ReceiptVerification
from pynchy.host.orchestrator.api import prepare_action_intent
from pynchy.plugins.api import HostActionCatalog
from pynchy.state import init_test_database
from pynchy.workspace.api import (
    CapabilityRule,
    ResolvedWorkspaceConfig,
    ServiceTrustConfig,
    WorkspaceSecurity,
)
from tests.action_intents_support import _transactional_action

_GROUP = "service-edge"


@pytest.fixture(autouse=True)
async def _setup():
    await init_test_database()
    clear_plugin_handler_cache()
    yield
    destroy_gate(_GROUP, 1000.0)


def _request(tool: str, request_id: str = "request-1", **extra: object) -> dict[str, object]:
    return {"type": f"service:{tool}", "request_id": request_id, **extra}


def _catalog(tool: str, handler: AsyncMock):
    return make_host_action_catalog(tool, handler=handler)


def _safe_gate(tool: str, *, capabilities: dict[str, CapabilityRule] | None = None) -> None:
    create_gate(
        _GROUP,
        1000.0,
        WorkspaceSecurity(
            services={tool: ServiceTrustConfig(dangerous_writes=False)},
            capabilities=capabilities or {"*": CapabilityRule("allow")},
        ),
    )


def _resolved(*tools: str) -> ResolvedWorkspaceConfig:
    return ResolvedWorkspaceConfig(
        skills=[],
        tools=list(tools),
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
    )


@pytest.mark.asyncio
async def test_route_activation_between_policy_checks_fails_closed(tmp_path):
    handler = AsyncMock(return_value={"result": "must not run"})
    tool = "route_edge"
    settings = make_settings(data_dir=tmp_path)
    deps = NullIpcDeps()

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, handler),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.load_resolved_config",
            side_effect=[None, None],
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_active_matrix_route",
            side_effect=[None, object()],
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"),
    ):
        await registry.dispatch(_request(tool), _GROUP, False, deps)

    handler.assert_not_awaited()
    response = (tmp_path / "ipc" / _GROUP / "responses/request-1.json").read_text()
    assert "security policy is unavailable" in response


@pytest.mark.asyncio
async def test_active_route_without_resolved_policy_is_rejected(tmp_path):
    tool = "route_unavailable_edge"
    handler = AsyncMock(return_value={"result": "must not run"})
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, handler),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.load_resolved_config",
            return_value=None,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_active_matrix_route",
            return_value=object(),
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"),
    ):
        await registry.dispatch(_request(tool), _GROUP, False, NullIpcDeps())

    handler.assert_not_awaited()
    assert (
        "not enabled for this route"
        in (tmp_path / "ipc" / _GROUP / "responses/request-1.json").read_text()
    )


@pytest.mark.asyncio
async def test_resolved_workspace_without_registered_gate_uses_ephemeral_policy(tmp_path):
    handler = AsyncMock(return_value={"result": "ok"})
    tool = "ephemeral_edge"
    settings = make_settings(data_dir=tmp_path)
    safe_security = WorkspaceSecurity(
        capabilities={"*": CapabilityRule("allow")},
        services={tool: ServiceTrustConfig(dangerous_writes=False)},
    )

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, handler),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.load_resolved_config",
            return_value=_resolved(tool),
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.build_workspace_security",
            return_value=safe_security,
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"),
    ):
        await registry.dispatch(_request(tool), _GROUP, False, NullIpcDeps())

    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_action_intent_approval_marks_intent_awaiting_approval(tmp_path):
    tool = "test_transactional_send"
    action = _transactional_action(AsyncMock())
    create_gate(
        _GROUP,
        1000.0,
        WorkspaceSecurity(
            services={tool: ServiceTrustConfig(dangerous_writes=True)},
        ),
    )
    deps = NullIpcDeps()
    intent, replay = await prepare_action_intent(
        action,
        {"room_id": "!room:test", "body": "x"},
        workspace=_GROUP,
        chat_jid="unknown",
        request_id="request-1",
    )
    assert intent is not None
    assert replay is None
    deps.prepare_action_intent = AsyncMock(return_value=(intent, None))
    deps.mark_action_intent_awaiting_approval = AsyncMock()
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=HostActionCatalog(actions=(action,)),
        ),
        patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            tmp_path / "approvals",
        ),
    ):
        await registry.dispatch(_request(tool), _GROUP, False, deps)

    deps.mark_action_intent_awaiting_approval.assert_awaited_once_with(
        "request-1",
        policy_decision=(
            "Capability 'test.transactional.send' requires human approval by default; "
            "human confirmation"
        ),
    )
    assert not (tmp_path / "ipc" / _GROUP / "responses/request-1.json").exists()


@pytest.mark.asyncio
async def test_replayed_action_response_is_written_without_execution(tmp_path):
    tool = "replay_edge"
    handler = AsyncMock()
    _safe_gate(tool)
    deps = NullIpcDeps()
    deps.prepare_action_intent = AsyncMock(return_value=(None, {"result": "replayed"}))
    deps.execute_action_intent = AsyncMock()
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, handler),
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"),
    ):
        await registry.dispatch(_request(tool), _GROUP, False, deps)

    deps.execute_action_intent.assert_not_awaited()
    assert json.loads((tmp_path / "ipc" / _GROUP / "responses/request-1.json").read_text()) == {
        "result": "replayed"
    }


@pytest.mark.asyncio
async def test_denied_action_intent_is_closed_before_provider_dispatch(tmp_path):
    tool = "deny_intent_edge"
    handler = AsyncMock()
    action = _catalog(tool, handler).action_for(tool)
    assert action is not None
    _safe_gate(tool, capabilities={str(action.capability.id): CapabilityRule(decision="deny")})
    deps = NullIpcDeps()
    deps.prepare_action_intent = AsyncMock(return_value=(object(), None))
    deps.deny_action_intent = AsyncMock()
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, handler),
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"),
    ):
        await registry.dispatch(_request(tool), _GROUP, False, deps)

    deps.deny_action_intent.assert_awaited_once()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_action_intent_is_approved_before_provider_dispatch(tmp_path):
    tool = "approve_intent_edge"
    handler = AsyncMock(return_value={"result": "ok"})
    _safe_gate(tool)
    deps = NullIpcDeps()
    deps.prepare_action_intent = AsyncMock(return_value=(object(), None))
    deps.approve_action_intent = AsyncMock()
    deps.execute_action_intent = AsyncMock(return_value={"result": "ok"})
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, handler),
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"),
    ):
        await registry.dispatch(_request(tool), _GROUP, False, deps)

    deps.approve_action_intent.assert_awaited_once()
    deps.execute_action_intent.assert_awaited_once()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_exception_is_audited_and_re_raised(tmp_path):
    tool = "provider_failure_edge"
    _safe_gate(tool)
    deps = NullIpcDeps()
    deps.execute_action_intent = AsyncMock(side_effect=RuntimeError("provider failed"))
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, AsyncMock()),
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.record_security_event",
            new_callable=AsyncMock,
        ) as audit,
        pytest.raises(RuntimeError, match="provider failed"),
    ):
        await registry.dispatch(_request(tool), _GROUP, False, deps)

    assert [call.kwargs["decision"] for call in audit.await_args_list] == [
        "allowed",
        "execution_failed",
    ]


def _mcp_settings(tmp_path, tool: str):
    settings = MagicMock()
    settings.data_dir = tmp_path
    settings.tools = {
        tool: McpTool(
            type="mcp",
            mcp=McpToolConfig(runtime="script", command="run-tool", port=9000),
        )
    }
    return settings


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [ReceiptVerification.INVALID, ReceiptVerification.VALID])
async def test_script_mcp_receipt_outcomes_are_fail_closed(tmp_path, receipt):
    tool = "script_receipt_edge"
    handler = AsyncMock(return_value={"result": "ok"})
    _safe_gate(tool)
    settings = _mcp_settings(tmp_path, tool)
    deps = NullIpcDeps()

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, handler),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=receipt,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ) as cop,
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", tmp_path / "ipc"),
    ):
        await registry.dispatch(_request(tool, value="payload"), _GROUP, False, deps)

    if receipt is ReceiptVerification.INVALID:
        handler.assert_not_awaited()
        cop.assert_not_awaited()
        assert (
            "Invalid or replayed"
            in (tmp_path / "ipc" / _GROUP / "responses/request-1.json").read_text()
        )
    else:
        handler.assert_awaited_once()
        cop.assert_not_awaited()


@pytest.mark.asyncio
async def test_script_mcp_absent_receipt_enters_cop_gate(tmp_path):
    tool = "script_absent_edge"
    handler = AsyncMock(return_value={"result": "ok"})
    _safe_gate(tool)
    settings = _mcp_settings(tmp_path, tool)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
            return_value=_catalog(tool, handler),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=ReceiptVerification.ABSENT,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=True,
        ) as cop,
    ):
        await registry.dispatch(_request(tool, value="payload"), _GROUP, False, NullIpcDeps())

    handler.assert_awaited_once()
    cop.assert_awaited_once()
    assert "args:" in cop.call_args.args[1]
