"""Public capability-status edge contracts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from pynchy.actions.api import ActionId
from pynchy.host.orchestrator.capability_status import (
    CapabilityPolicyDecision,
    CapabilityStatusOperations,
    WorkspaceCapabilityConfiguration,
    canary_outcomes_from_report,
    collect_capability_status,
    resolve_workspace_capabilities,
)
from pynchy.plugins.api import (
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityStatus,
    HostActionAccess,
    HostActionCatalog,
    HostActionDescriptor,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.workspace.api import WorkspaceSecurity


def _action(*, probe=None) -> HostActionDescriptor:
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId("test.capability"),
            kind=CapabilityKind.HOST_ACTION,
            owner="tests",
            summary="Test capability",
            action_ids=(ActionId("test.action"),),
            probe=probe,
        ),
        tool_name=HostToolName("test-tool"),
        handler=AsyncMock(),
        access=HostActionAccess.READ,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
        audit=AuditContract(),
    )


def _operations(*, workspaces: tuple[str, ...] = ("test",)) -> CapabilityStatusOperations:
    return CapabilityStatusOperations(
        workspaces=workspaces,
        workspace_configuration=lambda _workspace: WorkspaceCapabilityConfiguration(
            enabled_tools=frozenset({"test-tool"}),
            security=WorkspaceSecurity(),
        ),
        evaluate_action_policy=lambda _action, _security: CapabilityPolicyDecision(
            allowed=True,
            reason=None,
            approval_required=False,
            cop_review_required=False,
        ),
    )


def test_canary_report_parser_fails_closed_for_malformed_rows():
    assert canary_outcomes_from_report({"scenarios": "not-a-list"}) == {}
    assert canary_outcomes_from_report(
        {
            "scenarios": [
                "not-a-mapping",
                {"id": 123, "latest_runs": []},
                {"id": "missing-runs", "latest_runs": "not-a-list"},
                {"id": "failed", "latest_runs": [{"outcome": "passed"}, {"outcome": "failed"}]},
                {"id": "cleanup", "latest_runs": [{"outcome": "cleanup_failed"}]},
                {"id": "passed", "latest_runs": [{"outcome": "passed"}]},
                {"id": "other", "latest_runs": [{"outcome": "skipped"}]},
            ]
        }
    ) == {
        "failed": "failed",
        "cleanup": "failed",
        "passed": "passed",
        "other": "skipped",
    }


@pytest.mark.asyncio
async def test_probe_exception_becomes_unavailable_capability():
    async def broken_probe(_context):
        await asyncio.sleep(0)
        raise RuntimeError("probe crashed")

    action = _action(probe=broken_probe)
    snapshot = await resolve_workspace_capabilities(
        "test",
        operations=_operations(),
        catalog=HostActionCatalog(actions=(action,)),
    )

    capability = snapshot.capabilities[0]
    assert capability.status is CapabilityStatus.UNAVAILABLE
    assert capability.reason == "Availability probe failed: RuntimeError"


@pytest.mark.asyncio
async def test_collect_status_summarizes_sorted_workspace_snapshots():
    operations = _operations(workspaces=("zeta", "alpha"))
    result = await collect_capability_status(
        {"scenarios": []},
        operations=operations,
        catalog=HostActionCatalog(actions=(_action(),)),
    )

    assert result["summary"][CapabilityStatus.READY.value] == 2
    assert [item["workspace"] for item in result["workspaces"]] == ["alpha", "zeta"]
