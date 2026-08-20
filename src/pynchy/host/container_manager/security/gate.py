"""Worker-scoped security enforcement for all tool calls.

One SecurityGate per durable worker process, shared by IPC and MCP callers.
Wraps SecurityPolicy to provide sticky taint tracking and a uniform
evaluate interface.

Registry keys retain the worker invocation timestamp so direct-host turns and
container workers use the same IPC/MCP lookup contract.
"""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves security resolution annotations.
    Callable,
    Mapping,
    Sequence,
)
from typing import Any, Protocol, runtime_checkable

from pynchy.host.container_manager.security.middleware import PolicyDecision, SecurityPolicy
from pynchy.plugins.api import (
    ApprovalMode,
    ApprovalTrigger,
    HostActionAccess,
    HostActionDescriptor,
)
from pynchy.workspace.api import (
    CapabilityRule,
    ServiceTrustConfig,
    WorkspaceSecurity,
)

# ---------------------------------------------------------------------------
# SecurityGate
# ---------------------------------------------------------------------------


@runtime_checkable
class SecurityToolConfig(Protocol):
    type: str
    public_source: bool
    secret_data: bool
    public_sink: bool
    dangerous_writes: bool


@runtime_checkable
class ResolvedSecurityConfig(Protocol):
    @property
    def tools(self) -> Sequence[str]: ...

    @property
    def contains_secrets(self) -> bool: ...

    @property
    def cop_active(self) -> bool: ...

    @property
    def capabilities(self) -> Mapping[str, CapabilityRule]: ...


@runtime_checkable
class SecuritySettings(Protocol):
    @property
    def tools(self) -> Mapping[str, object]: ...


def _unconfigured_settings() -> SecuritySettings:
    raise RuntimeError("Security configuration has not been composed")


def _unconfigured_resolved_config(
    _source_group: str, _settings: SecuritySettings | None
) -> ResolvedSecurityConfig | None:
    raise RuntimeError("Security workspace resolution has not been composed")


_get_settings: Callable[[], SecuritySettings] = _unconfigured_settings
load_resolved_config: Callable[[str, SecuritySettings | None], ResolvedSecurityConfig | None] = (
    _unconfigured_resolved_config
)


def configure_security_resolution(
    *,
    get_settings: Callable[[], SecuritySettings],
    resolve_workspace_config: Callable[
        [str, SecuritySettings | None], ResolvedSecurityConfig | None
    ],
) -> None:
    """Bind the security policy projection at host composition."""
    global _get_settings, load_resolved_config  # noqa: PLW0603 - one host process owns these policy sources.
    _get_settings = get_settings
    load_resolved_config = resolve_workspace_config


def _security_tool(tool: object) -> SecurityToolConfig | None:
    if not isinstance(tool, SecurityToolConfig) or tool.type == "workspace":
        return None
    return tool


class SecurityGate:
    """Session-scoped security enforcement for all tool calls."""

    def __init__(self, security: WorkspaceSecurity) -> None:
        self._service_names = frozenset(security.services)
        self._policy = SecurityPolicy(security)
        self._session_tool_approvals: set[str] = set()
        self._session_capability_approvals: set[str] = set()

    @property
    def policy(self) -> SecurityPolicy:
        """Access the underlying SecurityPolicy (for taint inspection)."""
        return self._policy

    @property
    def service_names(self) -> frozenset[str]:
        """Return non-secret service aliases compiled from workspace configuration."""
        return self._service_names

    def evaluate_read(
        self,
        service: str,
        default_trust: ServiceTrustConfig | None = None,
    ) -> PolicyDecision:
        """Evaluate a read operation. Delegates to SecurityPolicy."""
        return self._policy.evaluate_read(service, default_trust)

    def evaluate_write(
        self,
        service: str,
        data: dict[str, Any],
        default_trust: ServiceTrustConfig | None = None,
    ) -> PolicyDecision:
        """Evaluate a write operation. Delegates to SecurityPolicy."""
        return self._policy.evaluate_write(service, data, default_trust)

    def evaluate_capability(self, capability: str) -> PolicyDecision:
        """Evaluate an explicit semantic capability."""
        return self._policy.evaluate_capability(capability)

    def notify_file_access(self) -> None:
        """Forward file-access notification to the policy."""
        self._policy.notify_file_access()

    def confirm_credential_access(self) -> None:
        """Retain a Cop-confirmed or conservatively confirmed secret exposure."""
        self._policy.confirm_credential_access()

    def notify_public_source_input(self) -> None:
        """Forward initial public-source taint to the policy."""
        self._policy.notify_public_source_input()

    def notify_secret_source_input(self) -> None:
        """Forward conservative private-input taint to the policy."""
        self._policy.notify_secret_source_input()

    def grant_session_tool_approval(self, tool_name: str) -> None:
        """Approve one opted-in host tool for this gate's session lifetime."""
        # Approval scope belongs to each tool: multi-step computer use should
        # prompt once, while one-shot effects such as sending email stay exact.
        self._session_tool_approvals.add(tool_name)

    def has_session_tool_approval(self, tool_name: str) -> bool:
        """Return whether this session already approved the opted-in tool."""
        return tool_name in self._session_tool_approvals

    def grant_session_capability_approval(self, capability_id: str) -> None:
        """Approve one semantic capability for this gate's session lifetime."""
        self._session_capability_approvals.add(capability_id)

    def has_session_capability_approval(self, capability_id: str) -> bool:
        """Return whether this session approved the semantic capability."""
        return capability_id in self._session_capability_approvals


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
        gate.evaluate_read(action.service_name, action.default_service_trust)
        if action.access is HostActionAccess.READ
        else gate.evaluate_write(action.service_name, data, action.default_service_trust)
    )
    tool_name = str(action.tool_name)
    trigger = action.approval.trigger
    needs_human = (
        capability.needs_human
        or trigger is ApprovalTrigger.ALWAYS
        or (
            trigger is ApprovalTrigger.SERVICE_POLICY
            and service.needs_human
            and not capability.overrides_human_approval
        )
    )
    approval_granted = (
        service.allowed
        and needs_human
        and trigger is not ApprovalTrigger.ALWAYS
        and (
            (
                action.approval.mode is ApprovalMode.SESSION_TOOL
                and gate.has_session_tool_approval(tool_name)
            )
            or gate.has_session_capability_approval(str(action.capability.id))
        )
    )
    reasons = [reason for reason in (capability.reason, service.reason) if reason]
    if (
        trigger is ApprovalTrigger.SERVICE_POLICY
        and capability.overrides_human_approval
        and service.needs_human
    ):
        reasons.append("Human approval suppressed by explicit capability allow")
    if trigger is ApprovalTrigger.CAPABILITY_ONLY and service.needs_human:
        reasons.append("Automatic service approval suppressed by host-action contract")
    if trigger is ApprovalTrigger.ALWAYS:
        reasons.append("Host action requires exact human approval")
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
    secret_source_input: bool = False,
) -> SecurityGate:
    """Create and register a SecurityGate for a worker process."""
    gate = SecurityGate(security)
    if public_source_input:
        gate.notify_public_source_input()
    if secret_source_input:
        gate.notify_secret_source_input()
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
    """Remove a SecurityGate when its owning worker exits."""
    _gates.pop((source_group, invocation_ts), None)


# ---------------------------------------------------------------------------
# Security resolution -- shared by IPC handler and future MCP proxy
# ---------------------------------------------------------------------------


def resolve_security(source_group: str, *, is_admin: bool = False) -> WorkspaceSecurity:
    """Resolve security from selected tools and workspace secret state."""
    s = _get_settings()
    resolved = load_resolved_config(source_group, s)
    contains_secrets = resolved.contains_secrets if resolved is not None else False

    del is_admin
    if resolved is None:
        return WorkspaceSecurity(contains_secrets=contains_secrets)

    return build_workspace_security(s, resolved)


def build_workspace_security(
    settings: SecuritySettings,
    resolved: ResolvedSecurityConfig,
) -> WorkspaceSecurity:
    """Build dispatch-equivalent security from an already resolved workspace."""

    services: dict[str, ServiceTrustConfig] = {}
    integration_services: dict[str, list[ServiceTrustConfig]] = {}
    tools = settings.tools
    for tool_name in resolved.tools:
        tool = _security_tool(tools.get(tool_name))
        if tool is None:
            continue
        trust = ServiceTrustConfig(
            public_source=tool.public_source,
            secret_data=tool.secret_data,
            public_sink=tool.public_sink,
            dangerous_writes=tool.dangerous_writes,
        )
        services[tool_name] = trust
        if tool.type not in {"builtin", "mcp"}:
            integration_services.setdefault(tool.type, []).append(trust)

    # Built-in host actions use stable integration service names while their
    # credential-bearing tool names are operator-defined.
    for service_name, declarations in integration_services.items():
        if len(declarations) == 1:
            services[service_name] = declarations[0]

    return WorkspaceSecurity(
        services=services,
        contains_secrets=resolved.contains_secrets,
        cop_active=resolved.cop_active,
        capabilities=dict(resolved.capabilities),
    )
