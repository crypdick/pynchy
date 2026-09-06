"""Worker-scoped security enforcement for all tool calls.

One SecurityGate per durable worker process, shared by IPC and MCP callers.
Owns sticky taint, session approvals, and service and capability decisions.

Registry keys retain the worker invocation timestamp so direct-host turns and
container workers use the same IPC/MCP lookup contract.
"""

from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
)
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.plugins.api import (
    ApprovalMode,
    ApprovalTrigger,
    HostActionAccess,
    HostActionDescriptor,
)
from pynchy.secrets_scanner import scan_payload_for_secrets
from pynchy.workspace.api import (
    CapabilityRule,
    ResolvedWorkspaceConfig,
    ServiceTrustConfig,
    WorkspaceSecurity,
    capability_pattern_matches,
    most_restrictive_capability_rule,
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
class SecuritySettings(Protocol):
    @property
    def tools(self) -> Mapping[str, object]: ...


def _unconfigured_settings() -> SecuritySettings:
    raise RuntimeError("Security configuration has not been composed")


def _unconfigured_resolved_config(
    _source_group: str, _settings: SecuritySettings | None
) -> ResolvedWorkspaceConfig | None:
    raise RuntimeError("Security workspace resolution has not been composed")


_get_settings: Callable[[], SecuritySettings] = _unconfigured_settings
load_resolved_config: Callable[[str, SecuritySettings | None], ResolvedWorkspaceConfig | None] = (
    _unconfigured_resolved_config
)


def configure_security_resolution(
    *,
    get_settings: Callable[[], SecuritySettings],
    resolve_workspace_config: Callable[
        [str, SecuritySettings | None], ResolvedWorkspaceConfig | None
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


# Default trust for unknown services — maximally cautious
_UNKNOWN_SERVICE = ServiceTrustConfig()


@dataclass
class PolicyDecision:
    """Result of policy evaluation."""

    allowed: bool
    reason: str | None = None
    needs_cop: bool = False
    needs_human: bool = False
    # NOTE: Update docs/usage/security.md § Permissions if this override changes.
    overrides_human_approval: bool = False


class SecurityGate:
    """Worker-scoped policy, sticky taint, and session approval state."""

    def __init__(self, security: WorkspaceSecurity) -> None:
        self._services = security.services
        self._capabilities = security.capabilities
        self._workspace_contains_secrets = security.contains_secrets
        self._cop_active = security.cop_active
        self._corruption_tainted = False
        self._secret_tainted = False
        self._service_names = frozenset(security.services)
        self._session_tool_approvals: set[str] = set()
        self._session_capability_approvals: set[str] = set()

    @property
    def corruption_tainted(self) -> bool:
        return self._corruption_tainted

    @property
    def secret_tainted(self) -> bool:
        return self._secret_tainted

    @property
    def cop_active(self) -> bool:
        """Return whether this workspace uses the secondary Cop reviewer."""
        return self._cop_active

    def _get_trust(
        self,
        service: str,
        default_trust: ServiceTrustConfig | None = None,
    ) -> ServiceTrustConfig:
        return self._services.get(service, default_trust or _UNKNOWN_SERVICE)

    def notify_file_access(self) -> None:
        """Called when the agent uses file-access tools (Read, Execute, Bash).

        Sets secret taint when the workspace declares ``contains_secrets``.
        Heuristic credential-path matches require separate Cop adjudication.
        """
        if self._workspace_contains_secrets:
            self._secret_tainted = True

    def confirm_credential_access(self) -> None:
        """Set sticky secret taint after credential evidence is confirmed."""
        self._secret_tainted = True

    def notify_public_source_input(self) -> None:
        """Mark the invocation as having received provider-controlled input."""
        self._corruption_tainted = True

    def notify_secret_source_input(self) -> None:
        """Mark input itself as private data, independent of workspace files."""
        self._secret_tainted = True

    def evaluate_read(
        self,
        service: str,
        default_trust: ServiceTrustConfig | None = None,
    ) -> PolicyDecision:
        """Evaluate a read operation on a service.

        - forbidden -> blocked
        - public_source=True -> cop scan, corruption taint set
        - public_source=False -> no gating
        - secret_data=True -> secret taint set (always, on any read)
        """
        trust = self._get_trust(service, default_trust)

        if trust.public_source == "forbidden":
            return PolicyDecision(
                allowed=False,
                reason=f"Reading from '{service}' is forbidden",
            )

        # Secret taint: set on any read from a service with secret_data
        if trust.secret_data:
            self._secret_tainted = True

        if trust.public_source:
            self._corruption_tainted = True
            if not self._cop_active:
                return PolicyDecision(allowed=True)
            return PolicyDecision(
                allowed=True,
                reason=f"Public source '{service}': cop scan required",
                needs_cop=True,
            )

        return PolicyDecision(allowed=True)

    def evaluate_capability(self, capability: str) -> PolicyDecision:
        """Evaluate an explicit semantic capability rule.

        Capability IDs use dotted segments and support trailing ``.*``
        wildcards, for example ``mcp.email.send`` and ``mcp.email.*``.
        Missing rules require human approval.
        """
        rule = self._matching_capability_rule(capability)
        if rule is None:
            return PolicyDecision(
                allowed=True,
                reason=f"Capability '{capability}' requires human approval by default",
                needs_human=True,
            )
        if rule.decision == "allow":
            return PolicyDecision(
                allowed=True,
                reason=f"Capability '{capability}' explicitly allowed",
                overrides_human_approval=True,
            )
        if rule.decision == "deny":
            return PolicyDecision(
                allowed=False,
                reason=f"Capability '{capability}' denied by policy",
            )
        return PolicyDecision(
            allowed=True,
            reason=f"Capability '{capability}' requires human approval",
            needs_human=True,
        )

    def evaluate_write(
        self,
        service: str,
        data: dict[str, Any],
        default_trust: ServiceTrustConfig | None = None,
    ) -> PolicyDecision:
        """Evaluate a write operation on a service.

        Checks forbidden first, then derives gating from the matrix:
        - Cop: corruption_tainted (any write by potentially-hijacked agent)
        - Human: dangerous_writes=True OR (corruption + secret + public_sink)
        """
        trust = self._get_trust(service, default_trust)
        forbidden = self._forbidden_write_decision(service, trust)
        if forbidden is not None:
            return forbidden

        scan_result = scan_payload_for_secrets(data)
        needs_cop, needs_human = self._write_gate_flags(
            trust,
            payload_has_secrets=scan_result.secrets_found,
        )
        reason = self._write_reason(
            needs_cop=needs_cop,
            needs_human=needs_human,
            detected_secrets=scan_result.detected,
        )

        return PolicyDecision(
            allowed=True,
            reason=reason,
            needs_cop=needs_cop,
            needs_human=needs_human,
        )

    def _matching_capability_rule(self, capability: str) -> CapabilityRule | None:
        return most_restrictive_capability_rule(
            rule
            for pattern, rule in self._capabilities.items()
            if capability_pattern_matches(pattern, capability)
        )

    def _forbidden_write_decision(
        self,
        service: str,
        trust: ServiceTrustConfig,
    ) -> PolicyDecision | None:
        if trust.public_sink == "forbidden":
            return PolicyDecision(
                allowed=False,
                reason=f"Writing to '{service}' is forbidden (public_sink)",
            )
        if trust.dangerous_writes == "forbidden":
            return PolicyDecision(
                allowed=False,
                reason=f"Writing to '{service}' is forbidden (dangerous_writes)",
            )
        return None

    def _write_gate_flags(
        self,
        trust: ServiceTrustConfig,
        *,
        payload_has_secrets: bool,
    ) -> tuple[bool, bool]:
        needs_cop = self._cop_active and self._corruption_tainted
        needs_human = bool(trust.dangerous_writes)

        if self._corruption_tainted and self._secret_tainted and trust.public_sink:
            needs_human = True
        if payload_has_secrets:
            needs_human = True

        return needs_cop, needs_human

    def _write_reason(
        self,
        *,
        needs_cop: bool,
        needs_human: bool,
        detected_secrets: list[str],
    ) -> str | None:
        reason_parts = []
        if needs_cop:
            reason_parts.append("cop (corruption taint)")
        if needs_human:
            reason_parts.append("human confirmation")
        if detected_secrets:
            reason_parts.append(f"secrets detected in payload ({', '.join(detected_secrets)})")
        return "; ".join(reason_parts) if reason_parts else None

    @property
    def service_names(self) -> frozenset[str]:
        """Return non-secret service aliases compiled from workspace configuration."""
        return self._service_names

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
    resolved: ResolvedWorkspaceConfig,
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
