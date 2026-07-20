"""Session-scoped security enforcement for all tool calls.

One SecurityGate per container invocation, shared by IPC and MCP callers.
Wraps SecurityPolicy to provide sticky taint tracking and a uniform
evaluate interface.

Registry keyed by (group_folder, invocation_ts) to support future
concurrent containers for the same group.
"""

from __future__ import annotations

from typing import Any

import pynchy.config as pynchy_config
import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.capabilities import ApprovalMode, HostActionAccess, HostActionDescriptor
from pynchy.config import Settings  # noqa: TC001, RUF100 - beartype resolves runtime annotations.
from pynchy.config.merge import (  # noqa: TC001, RUF100 - beartype resolves runtime annotations.
    ResolvedWorkspaceConfig,
)
from pynchy.host.container_manager.security.middleware import PolicyDecision, SecurityPolicy
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity

# ---------------------------------------------------------------------------
# SecurityGate
# ---------------------------------------------------------------------------


class SecurityGate:
    """Session-scoped security enforcement for all tool calls."""

    def __init__(self, security: WorkspaceSecurity) -> None:
        self._policy = SecurityPolicy(security)
        self._session_tool_approvals: set[str] = set()

    @property
    def policy(self) -> SecurityPolicy:
        """Access the underlying SecurityPolicy (for taint inspection)."""
        return self._policy

    def evaluate_read(self, service: str) -> PolicyDecision:
        """Evaluate a read operation. Delegates to SecurityPolicy."""
        return self._policy.evaluate_read(service)

    def evaluate_write(self, service: str, data: dict[str, Any]) -> PolicyDecision:
        """Evaluate a write operation. Delegates to SecurityPolicy."""
        return self._policy.evaluate_write(service, data)

    def evaluate_capability(self, capability: str) -> PolicyDecision:
        """Evaluate an explicit semantic capability."""
        return self._policy.evaluate_capability(capability)

    def notify_file_access(self, *, credential_access: bool = False) -> None:
        """Forward file-access notification to the policy."""
        self._policy.notify_file_access(credential_access=credential_access)

    def notify_public_source_input(self) -> None:
        """Forward initial public-source taint to the policy."""
        self._policy.notify_public_source_input()

    def grant_session_tool_approval(self, tool_name: str) -> None:
        """Approve one opted-in host tool for this gate's session lifetime."""
        # Approval scope belongs to each tool: multi-step computer use should
        # prompt once, while one-shot effects such as sending email stay exact.
        self._session_tool_approvals.add(tool_name)

    def has_session_tool_approval(self, tool_name: str) -> bool:
        """Return whether this session already approved the opted-in tool."""
        return tool_name in self._session_tool_approvals


def evaluate_host_action_policy(
    action: HostActionDescriptor,
    gate: SecurityGate,
    data: dict[str, Any],
) -> PolicyDecision:
    """Compose semantic capability policy with the service-trust decision."""
    capability = gate.evaluate_capability(str(action.capability.id))
    if not capability.allowed:
        return capability
    service = (
        gate.evaluate_read(action.service_name)
        if action.access is HostActionAccess.READ
        else gate.evaluate_write(action.service_name, data)
    )
    tool_name = str(action.tool_name)
    needs_human = capability.needs_human or (
        service.needs_human and not capability.overrides_human_approval
    )
    approval_granted = (
        service.allowed
        and needs_human
        and action.approval.mode is ApprovalMode.SESSION_TOOL
        and gate.has_session_tool_approval(tool_name)
    )
    reasons = [reason for reason in (capability.reason, service.reason) if reason]
    if capability.overrides_human_approval and service.needs_human:
        reasons.append("Human approval suppressed by explicit capability allow")
    if approval_granted:
        reasons.append(f"Session approval active for tool '{tool_name}'")
    return PolicyDecision(
        allowed=service.allowed,
        reason="; ".join(reasons) or None,
        needs_cop=service.needs_cop,
        needs_human=needs_human and not approval_granted,
    )


# ---------------------------------------------------------------------------
# Registry -- keyed by (group_folder, invocation_ts)
# ---------------------------------------------------------------------------

_gates: dict[tuple[str, float], SecurityGate] = {}


def create_gate(
    source_group: str,
    invocation_ts: float,
    security: WorkspaceSecurity,
    *,
    public_source_input: bool = False,
) -> SecurityGate:
    """Create and register a SecurityGate for a container invocation."""
    gate = SecurityGate(security)
    if public_source_input:
        gate.notify_public_source_input()
    _gates[source_group, invocation_ts] = gate
    return gate


def get_gate(source_group: str, invocation_ts: float) -> SecurityGate | None:
    """Look up a SecurityGate by group and invocation timestamp."""
    return _gates.get((source_group, invocation_ts))


def get_gate_for_group(source_group: str) -> SecurityGate | None:
    """Look up a SecurityGate by group folder only (returns the latest).

    Used by IPC handlers that don't have the invocation_ts.
    When multiple gates exist for the same group (concurrent containers),
    returns the one with the highest timestamp.
    """
    matches = [(ts, g) for (grp, ts), g in _gates.items() if grp == source_group]
    if not matches:
        return None
    return max(matches, key=lambda x: x[0])[1]


def destroy_gate(source_group: str, invocation_ts: float) -> None:
    """Remove a SecurityGate when its container exits."""
    _gates.pop((source_group, invocation_ts), None)


# ---------------------------------------------------------------------------
# Security resolution -- shared by IPC handler and future MCP proxy
# ---------------------------------------------------------------------------


def resolve_security(source_group: str, *, is_admin: bool = False) -> WorkspaceSecurity:
    """Resolve security from selected tools and workspace secret state."""
    s = pynchy_config.get_settings()
    config_group = workspace_config.static_workspace_folder(source_group)
    resolve_workspace = getattr(s, "resolved_workspace_config", None)
    resolved = resolve_workspace(config_group) if callable(resolve_workspace) else None
    contains_secrets = resolved.contains_secrets if resolved is not None else False

    del is_admin
    if resolved is None:
        return WorkspaceSecurity(contains_secrets=contains_secrets)

    return build_workspace_security(s, resolved)


def build_workspace_security(
    settings: Settings,
    resolved: ResolvedWorkspaceConfig,
) -> WorkspaceSecurity:
    """Build dispatch-equivalent security from an already resolved workspace."""

    services: dict[str, ServiceTrustConfig] = {}
    tools = settings.tools
    for tool_name in resolved.tools:
        tool = tools.get(tool_name)
        if tool is None:
            continue
        services[tool_name] = ServiceTrustConfig(
            public_source=tool.public_source,
            secret_data=tool.secret_data,
            public_sink=tool.public_sink,
            dangerous_writes=tool.dangerous_writes,
        )

    return WorkspaceSecurity(
        services=services,
        contains_secrets=resolved.contains_secrets,
        capabilities=dict(resolved.capabilities),
    )
