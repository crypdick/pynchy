"""Workspace resolution tests for typed host-action capabilities."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

import pynchy.host.container_manager.ipc.registry as registry
from pynchy import state
from pynchy.actions.api import (
    ActionId,
    ActionSpec,
    ActionSurface,
    ActionTransport,
    EvidenceRequirement,
)
from pynchy.config.api import (
    BuiltinTool,
    LinearTool,
    ProfileConfig,
    Settings,
    WorkspaceConfig,
    apply_tool_access,
)
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    build_workspace_security,
    create_gate,
    destroy_gate,
    evaluate_host_action_policy,
)
from pynchy.host.orchestrator.capability_status import (
    CapabilityPolicyDecision,
    CapabilityStatusOperations,
    WorkspaceCapabilityConfiguration,
    collect_capability_status,
    resolve_workspace_capabilities,
)
from pynchy.plugins.api import (
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityProbeResult,
    CapabilityRequirement,
    CapabilityRequirementKind,
    CapabilityStatus,
    HostActionAccess,
    HostActionCatalog,
    HostActionDescriptor,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    ProbeStatus,
)
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
from pynchy.workspace.api import (
    CapabilityRule,
    WorkspaceProfile,
    WorkspaceSecurity,
)

_TOOL_NAME = "matrix_route_read"
_ACTION_ID = "chat.matrix.route.read"


async def _handler(_data: dict) -> dict[str, object]:
    return await asyncio.sleep(0, result={"result": "ok"})


def _action(
    *,
    action_id: str = _ACTION_ID,
    probe_status: ProbeStatus | None = None,
    required_workspace_tool: str | None = None,
) -> HostActionDescriptor:
    async def probe(_context) -> CapabilityProbeResult:
        assert probe_status is not None
        return await asyncio.sleep(
            0,
            result=CapabilityProbeResult(probe_status, f"probe is {probe_status.value}"),
        )

    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(_ACTION_ID),
            kind=CapabilityKind.HOST_ACTION,
            owner="tests",
            summary="List Matrix chats.",
            action_ids=(ActionId(action_id),),
            requirements=(
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                    name=required_workspace_tool,
                    description=f"Enable the {required_workspace_tool} integration.",
                ),
            )
            if required_workspace_tool is not None
            else (),
            probe=probe if probe_status is not None else None,
        ),
        tool_name=HostToolName(_TOOL_NAME),
        handler=_handler,
        access=HostActionAccess.READ,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
        audit=AuditContract(),
    )


def _settings(
    *,
    enabled: bool = True,
    decision: str | None = None,
    tools: list[str] | None = None,
):
    permissions = (
        {"ask" if decision == "needs_human" else decision: [_ACTION_ID]}
        if decision is not None
        else {}
    )
    selected_tools = tools if tools is not None else [_TOOL_NAME] if enabled else []
    return make_settings(
        tools={name: BuiltinTool(type="builtin") for name in selected_tools},
        profiles={
            "matrix": ProfileConfig(
                tools=selected_tools,
                permissions=permissions,
            )
        },
        workspaces={"test-ws": WorkspaceConfig(profiles=["matrix"])},
    )


def _catalog(
    action: HostActionDescriptor,
    *,
    action_specs: tuple[ActionSpec, ...] | None = None,
) -> HostActionCatalog:
    return HostActionCatalog(
        actions=(action,),
        action_specs=action_specs or HostActionCatalog(actions=()).action_specs,
    )


def _operations(settings: Settings) -> CapabilityStatusOperations:
    def workspace_configuration(workspace: str) -> WorkspaceCapabilityConfiguration | None:
        resolved = settings.resolved_workspace_config(workspace)
        if resolved is None:
            return None
        resolved = apply_tool_access(settings.tools, resolved)[0]
        return WorkspaceCapabilityConfiguration(
            enabled_tools=frozenset(resolved.tools),
            security=build_workspace_security(settings, resolved),
        )

    def evaluate_action_policy(
        action: HostActionDescriptor,
        security: WorkspaceSecurity,
    ) -> CapabilityPolicyDecision:
        decision = evaluate_host_action_policy(action, SecurityGate(security), {})
        return CapabilityPolicyDecision(
            allowed=decision.allowed,
            reason=decision.reason,
            approval_required=decision.needs_human,
            cop_review_required=decision.needs_cop,
        )

    return CapabilityStatusOperations(
        workspaces=tuple(settings.workspaces),
        workspace_configuration=workspace_configuration,
        evaluate_action_policy=evaluate_action_policy,
    )


async def _resolved_status(
    *,
    action: HostActionDescriptor | None = None,
    settings=None,
    outcomes: dict[str, str] | None = None,
    action_specs: tuple[ActionSpec, ...] | None = None,
):
    effective_settings = settings or _settings()
    snapshot = await resolve_workspace_capabilities(
        "test-ws",
        operations=_operations(effective_settings),
        catalog=_catalog(action or _action(), action_specs=action_specs),
        canary_outcomes=outcomes,
    )
    return snapshot.capabilities[0]


@pytest.mark.asyncio
async def test_ready_capability_includes_current_policy_requirements():
    resolved = await _resolved_status()

    assert resolved.status is CapabilityStatus.READY
    assert resolved.cop_review_required is True
    assert resolved.approval_required is True


@pytest.mark.asyncio
async def test_missing_workspace_or_tool_is_unconfigured():
    settings = _settings()
    missing_workspace = await resolve_workspace_capabilities(
        "missing",
        operations=_operations(settings),
        catalog=_catalog(_action()),
    )
    missing_tool = await _resolved_status(settings=_settings(enabled=False))

    assert missing_workspace.capabilities[0].status is CapabilityStatus.UNCONFIGURED
    assert missing_tool.status is CapabilityStatus.UNCONFIGURED


@pytest.mark.asyncio
async def test_declared_workspace_requirement_controls_action_readiness():
    action = _action(required_workspace_tool="linear")

    ready = await _resolved_status(action=action, settings=_settings(tools=["linear"]))
    missing = await _resolved_status(action=action, settings=_settings(tools=[]))

    assert ready.status is CapabilityStatus.READY
    assert missing.status is CapabilityStatus.UNCONFIGURED
    assert missing.reason == "Tool linear is not enabled for this workspace"


@pytest.mark.asyncio
async def test_named_linear_account_satisfies_stable_host_action_requirement(monkeypatch):
    """A credential-bearing account name must still expose the Linear capability."""
    monkeypatch.setenv("LINEAR_API_KEY", "configured")
    settings = make_settings(
        profiles={"synapse": ProfileConfig(tools=["linear_synapse"])},
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
    action = next(
        action
        for action in host_action_registration().actions
        if action.tool_name == "linear_list_work_items"
    )

    snapshot = await resolve_workspace_capabilities(
        "test-ws",
        operations=_operations(settings),
        catalog=HostActionCatalog(actions=(action,), action_specs=()),
    )

    assert snapshot.capabilities[0].status is CapabilityStatus.READY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_status", "expected"),
    [
        (ProbeStatus.UNAVAILABLE, CapabilityStatus.UNAVAILABLE),
        (ProbeStatus.DEGRADED, CapabilityStatus.DEGRADED),
    ],
)
async def test_probe_outcomes_have_distinct_statuses(probe_status, expected):
    resolved = await _resolved_status(action=_action(probe_status=probe_status))

    assert resolved.status is expected
    assert resolved.reason == f"probe is {probe_status.value}"


@pytest.mark.asyncio
async def test_profile_capability_denial_is_distinct_from_missing_config():
    resolved = await _resolved_status(settings=_settings(decision="deny"))

    assert resolved.status is CapabilityStatus.DENIED_BY_POLICY
    assert resolved.reason == "Capability 'chat.matrix.route.read' denied by policy"


def _agentic_action_spec() -> ActionSpec:
    return ActionSpec(
        id=ActionId(_ACTION_ID),
        owner="tests",
        summary="List Matrix chats with real-service evidence.",
        test_requirement=EvidenceRequirement.HERMETIC_AND_AGENTIC,
        canary_scenario="chat.matrix.round.trip",
        surfaces=(ActionSurface(ActionTransport.AGENT_TOOL, _TOOL_NAME),),
    )


@pytest.mark.asyncio
async def test_agentic_evidence_distinguishes_not_established_failed_and_ready():
    specs = (_agentic_action_spec(),)
    not_established = await _resolved_status(action_specs=specs)
    failed = await _resolved_status(
        action_specs=specs,
        outcomes={"chat.matrix.round.trip": "failed"},
    )
    ready = await _resolved_status(
        action_specs=specs,
        outcomes={"chat.matrix.round.trip": "passed"},
    )

    assert not_established.status is CapabilityStatus.NOT_ESTABLISHED
    assert failed.status is CapabilityStatus.DEGRADED
    assert ready.status is CapabilityStatus.READY


@pytest.mark.asyncio
async def test_status_collection_aggregates_workspace_snapshots():
    settings = _settings(enabled=False)
    result = await collect_capability_status(
        {},
        operations=_operations(settings),
        catalog=_catalog(_action()),
    )

    assert result["summary"]["unconfigured"] == 1
    assert result["workspaces"][0]["workspace"] == "test-ws"


class _Deps(NullIpcDeps):
    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return {
            "chat": WorkspaceProfile(
                jid="chat",
                name="Test",
                folder="test-ws",
                trigger="@Pynchy",
                added_at="2026-01-01",
            )
        }


@pytest.mark.asyncio
async def test_ready_snapshot_does_not_authorize_later_dispatch(tmp_path):
    """A stale ready display cannot bypass a dispatch-time policy change."""
    await state.init_test_database()
    mock_handler = AsyncMock(return_value={"result": "unsafe"})
    action = _action()
    action = HostActionDescriptor(
        capability=action.capability,
        tool_name=action.tool_name,
        handler=mock_handler,
        access=action.access,
        approval=action.approval,
        idempotency=action.idempotency,
        audit=action.audit,
    )
    catalog = _catalog(action)
    settings = _settings()
    snapshot = await resolve_workspace_capabilities(
        "test-ws",
        operations=_operations(settings),
        catalog=catalog,
    )
    assert snapshot.capabilities[0].status is CapabilityStatus.READY

    live_security = WorkspaceSecurity(capabilities={_ACTION_ID: CapabilityRule(decision="deny")})
    create_gate("test-ws", 1000.0, live_security)
    dispatch_settings = _settings()
    dispatch_settings.__dict__["data_dir"] = tmp_path
    try:
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
                return_value=catalog,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_service.get_settings",
                return_value=dispatch_settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir",
                dispatch_settings.data_dir / "ipc",
            ),
        ):
            await registry.dispatch(
                {
                    "type": f"service:{_TOOL_NAME}",
                    "request_id": "stale-ready",
                },
                "test-ws",
                False,
                _Deps(),
            )
    finally:
        destroy_gate("test-ws", 1000.0)

    mock_handler.assert_not_awaited()
    response = json.loads((tmp_path / "ipc/test-ws/responses/stale-ready.json").read_text())
    assert "Policy denied" in response["error"]
