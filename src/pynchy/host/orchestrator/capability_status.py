"""Resolve host-action descriptors into workspace capability snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import (  # capability operations are stored at runtime.
    Callable,
    Mapping,
)
from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.actions.api import ActionId, ActionSpec, EvidenceRequirement
from pynchy.logger import logger
from pynchy.plugins.api import (
    CapabilityProbeContext,
    CapabilityStatus,
    HostActionCatalog,
    HostActionDescriptor,
    ProbeStatus,
    ResolvedCapability,
    WorkspaceCapabilitySnapshot,
    get_host_action_catalog,
    missing_workspace_tool,
)
from pynchy.workspace.api import (
    WorkspaceSecurity,
)


@dataclass(frozen=True)
class _ResolutionContext:
    workspace: str
    enabled_tools: frozenset[str]
    security: WorkspaceSecurity
    canary_outcomes: Mapping[str, str]
    action_specs: tuple[ActionSpec, ...]
    evaluate_action_policy: Callable[
        [HostActionDescriptor, WorkspaceSecurity], CapabilityPolicyDecision
    ]


@dataclass(frozen=True)
class CapabilityPolicyDecision:
    """Policy fields that affect the operator-facing capability projection."""

    allowed: bool
    reason: str | None
    approval_required: bool
    cop_review_required: bool


@dataclass(frozen=True)
class WorkspaceCapabilityConfiguration:
    """Resolved workspace data needed to project host-action capabilities."""

    enabled_tools: frozenset[str]
    security: WorkspaceSecurity


@dataclass(frozen=True)
class CapabilityStatusOperations:
    """Configuration and policy operations supplied by the composition root."""

    workspaces: tuple[str, ...]
    workspace_configuration: Callable[[str], WorkspaceCapabilityConfiguration | None]
    evaluate_action_policy: Callable[
        [HostActionDescriptor, WorkspaceSecurity], CapabilityPolicyDecision
    ]


async def resolve_workspace_capabilities(
    workspace: str,
    *,
    operations: CapabilityStatusOperations,
    catalog: HostActionCatalog | None = None,
    canary_outcomes: Mapping[str, str] | None = None,
) -> WorkspaceCapabilitySnapshot:
    """Resolve every registered host action for one workspace."""
    effective_catalog = catalog or get_host_action_catalog()
    resolved_workspace = operations.workspace_configuration(workspace)
    outcomes = canary_outcomes or {}
    if resolved_workspace is None:
        capabilities = tuple(
            ResolvedCapability(
                descriptor=action.capability,
                status=CapabilityStatus.UNCONFIGURED,
                reason=f"Workspace {workspace!r} is not configured",
            )
            for action in effective_catalog.actions
        )
    else:
        context = _ResolutionContext(
            workspace=workspace,
            enabled_tools=resolved_workspace.enabled_tools,
            security=resolved_workspace.security,
            canary_outcomes=outcomes,
            action_specs=effective_catalog.action_specs,
            evaluate_action_policy=operations.evaluate_action_policy,
        )
        capabilities = tuple(
            await asyncio.gather(
                *(_resolve_action(action, context) for action in effective_catalog.actions)
            )
        )
    return WorkspaceCapabilitySnapshot(
        workspace=workspace,
        generated_at=datetime.now(UTC).isoformat(),
        capabilities=capabilities,
    )


async def collect_capability_status(
    canary_report: Mapping[str, object],
    *,
    operations: CapabilityStatusOperations,
    catalog: HostActionCatalog | None = None,
) -> dict[str, object]:
    """Return all configured workspace snapshots plus aggregate status counts."""
    effective_catalog = catalog or get_host_action_catalog()
    outcomes = canary_outcomes_from_report(canary_report)
    snapshots = await asyncio.gather(
        *(
            resolve_workspace_capabilities(
                workspace,
                operations=operations,
                catalog=effective_catalog,
                canary_outcomes=outcomes,
            )
            for workspace in sorted(operations.workspaces)
        )
    )
    summary = {status.value: 0 for status in CapabilityStatus}
    for snapshot in snapshots:
        for capability in snapshot.capabilities:
            summary[capability.status.value] += 1
    return {
        "summary": summary,
        "workspaces": [snapshot.to_dict() for snapshot in snapshots],
    }


def canary_outcomes_from_report(report: Mapping[str, object]) -> dict[str, str]:
    """Reduce safe canary report rows into one operational outcome per scenario."""
    raw_scenarios = report.get("scenarios", ())
    if not isinstance(raw_scenarios, list | tuple):
        return {}
    outcomes: dict[str, str] = {}
    for raw_scenario in raw_scenarios:
        if not isinstance(raw_scenario, Mapping):
            continue
        scenario_id = raw_scenario.get("id")
        latest_runs = raw_scenario.get("latest_runs", ())
        if not isinstance(scenario_id, str) or not isinstance(latest_runs, list | tuple):
            continue
        run_outcomes = [
            outcome
            for run in latest_runs
            if isinstance(run, Mapping) and isinstance((outcome := run.get("outcome")), str)
        ]
        if "failed" in run_outcomes or "cleanup_failed" in run_outcomes:
            outcomes[scenario_id] = "failed"
        elif "passed" in run_outcomes:
            outcomes[scenario_id] = "passed"
        elif run_outcomes:
            outcomes[scenario_id] = run_outcomes[0]
    return outcomes


async def _resolve_action(
    action: HostActionDescriptor,
    context: _ResolutionContext,
) -> ResolvedCapability:
    missing_tool = missing_workspace_tool(
        action,
        context.enabled_tools,
        service_aliases=context.security.services,
    )
    if missing_tool is not None:
        return ResolvedCapability(
            descriptor=action.capability,
            status=CapabilityStatus.UNCONFIGURED,
            reason=f"Tool {missing_tool} is not enabled for this workspace",
        )

    decision = context.evaluate_action_policy(action, context.security)
    if not decision.allowed:
        return ResolvedCapability(
            descriptor=action.capability,
            status=CapabilityStatus.DENIED_BY_POLICY,
            reason=decision.reason,
        )

    probe_result = None
    if action.capability.probe is not None:
        try:
            probe_result = await action.capability.probe(CapabilityProbeContext(context.workspace))
        except Exception as exc:  # noqa: BLE001 - each plugin probe is an isolation boundary.
            logger.exception(
                "Capability availability probe failed",
                capability_id=str(action.capability.id),
                workspace=context.workspace,
            )
            return ResolvedCapability(
                descriptor=action.capability,
                status=CapabilityStatus.UNAVAILABLE,
                reason=f"Availability probe failed: {type(exc).__name__}",
                approval_required=decision.approval_required,
                cop_review_required=decision.cop_review_required,
            )
        if probe_result.status is ProbeStatus.UNAVAILABLE:
            return ResolvedCapability(
                descriptor=action.capability,
                status=CapabilityStatus.UNAVAILABLE,
                reason=probe_result.reason,
                approval_required=decision.approval_required,
                cop_review_required=decision.cop_review_required,
            )

    scenarios = _required_canary_scenarios(
        action.capability.action_ids,
        context.action_specs,
    )
    evidence_status, evidence_reason = _evidence_status(
        scenarios,
        context.canary_outcomes,
    )
    if probe_result is not None and probe_result.status is ProbeStatus.DEGRADED:
        return ResolvedCapability(
            descriptor=action.capability,
            status=CapabilityStatus.DEGRADED,
            reason=probe_result.reason,
            approval_required=decision.approval_required,
            cop_review_required=decision.cop_review_required,
            canary_scenarios=scenarios,
        )
    return ResolvedCapability(
        descriptor=action.capability,
        status=evidence_status,
        reason=evidence_reason or decision.reason,
        approval_required=decision.approval_required,
        cop_review_required=decision.cop_review_required,
        canary_scenarios=scenarios,
    )


def _required_canary_scenarios(
    action_ids: tuple[ActionId, ...],
    action_specs: tuple[ActionSpec, ...],
) -> tuple[str, ...]:
    wanted = {str(action_id) for action_id in action_ids}
    return tuple(
        dict.fromkeys(
            spec.canary_scenario
            for spec in action_specs
            if str(spec.id) in wanted
            and spec.test_requirement is EvidenceRequirement.HERMETIC_AND_AGENTIC
            and spec.canary_scenario is not None
        )
    )


def _evidence_status(
    scenarios: tuple[str, ...],
    outcomes: Mapping[str, str],
) -> tuple[CapabilityStatus, str | None]:
    if not scenarios:
        return CapabilityStatus.READY, None
    if any(outcomes.get(scenario) == "failed" for scenario in scenarios):
        return CapabilityStatus.DEGRADED, "Required real-service evidence is regressing"
    missing = [scenario for scenario in scenarios if outcomes.get(scenario) != "passed"]
    if missing:
        return (
            CapabilityStatus.NOT_ESTABLISHED,
            f"Required canary evidence is not established: {', '.join(missing)}",
        )
    return CapabilityStatus.READY, None
