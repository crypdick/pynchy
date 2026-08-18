"""Behavioral tests for tool-scoped session approval grants."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from conftest import make_host_action_catalog

from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    evaluate_host_action_policy,
)
from pynchy.plugins.api import ApprovalContract, ApprovalMode, ApprovalTrigger, HostActionDescriptor
from pynchy.workspace.api import (
    CapabilityRule,
    ServiceTrustConfig,
    WorkspaceSecurity,
)


def _action(
    tool_name: str,
    *,
    approval_mode: ApprovalMode = ApprovalMode.EXACT_REQUEST,
) -> HostActionDescriptor:
    catalog = make_host_action_catalog(
        tool_name,
        handler=AsyncMock(),
        approval_mode=approval_mode,
    )
    action = catalog.action_for(tool_name)
    assert action is not None
    return action


def test_approved_tool_skips_later_human_gates_in_same_session() -> None:
    action = _action("computer_use", approval_mode=ApprovalMode.SESSION_TOOL)
    gate = SecurityGate(
        WorkspaceSecurity(
            services={"computer_use": ServiceTrustConfig(dangerous_writes=True)},
            capabilities={
                "test.computer.use": CapabilityRule(decision="needs_human"),
            },
        )
    )

    assert evaluate_host_action_policy(action, gate, {}).needs_human

    gate.grant_session_tool_approval("computer_use")

    decision = evaluate_host_action_policy(action, gate, {})
    assert decision.allowed
    assert not decision.needs_human
    assert "Session approval active" in (decision.reason or "")


def test_session_grant_does_not_approve_another_tool() -> None:
    gate = SecurityGate(
        WorkspaceSecurity(
            services={"send_email": ServiceTrustConfig(dangerous_writes=True)},
        )
    )
    gate.grant_session_tool_approval("computer_use")

    assert evaluate_host_action_policy(_action("send_email"), gate, {}).needs_human


def test_exact_request_tool_ignores_session_grant() -> None:
    action = _action("send_email")
    gate = SecurityGate(
        WorkspaceSecurity(
            services={"send_email": ServiceTrustConfig(dangerous_writes=True)},
        )
    )
    gate.grant_session_tool_approval("send_email")

    assert evaluate_host_action_policy(action, gate, {}).needs_human


def test_explicit_capability_allow_skips_service_human_gate() -> None:
    action = _action("computer_use", approval_mode=ApprovalMode.SESSION_TOOL)
    gate = SecurityGate(
        WorkspaceSecurity(
            services={"computer_use": ServiceTrustConfig(dangerous_writes=True)},
            capabilities={
                "test.computer.use": CapabilityRule(decision="allow"),
            },
        )
    )

    decision = evaluate_host_action_policy(action, gate, {})

    assert decision.allowed
    assert not decision.needs_human
    assert "explicitly allowed" in (decision.reason or "")
    assert "Human approval suppressed" in (decision.reason or "")


def test_capability_only_contract_explains_suppressed_service_approval() -> None:
    action = replace(
        _action("send_email"),
        approval=ApprovalContract(trigger=ApprovalTrigger.CAPABILITY_ONLY),
    )
    gate = SecurityGate(
        WorkspaceSecurity(
            services={"send_email": ServiceTrustConfig(dangerous_writes=True)},
            capabilities={"test.send.email": CapabilityRule("allow")},
        )
    )

    decision = evaluate_host_action_policy(action, gate, {})

    assert decision.allowed
    assert not decision.needs_human
    assert "Automatic service approval suppressed" in (decision.reason or "")


def test_explicit_capability_allow_cannot_override_service_forbidden() -> None:
    action = _action("computer_use", approval_mode=ApprovalMode.SESSION_TOOL)
    gate = SecurityGate(
        WorkspaceSecurity(
            services={
                "computer_use": ServiceTrustConfig(dangerous_writes="forbidden"),
            },
            capabilities={
                "test.computer.use": CapabilityRule(decision="allow"),
            },
        )
    )

    decision = evaluate_host_action_policy(action, gate, {})

    assert not decision.allowed
    assert not decision.needs_human


def test_session_grant_cannot_override_a_later_policy_denial() -> None:
    action = _action("computer_use")
    gate = SecurityGate(
        WorkspaceSecurity(
            capabilities={
                "test.computer.use": CapabilityRule(decision="deny"),
            },
        )
    )
    gate.grant_session_tool_approval("computer_use")

    decision = evaluate_host_action_policy(action, gate, {})
    assert not decision.allowed
    assert not decision.needs_human
