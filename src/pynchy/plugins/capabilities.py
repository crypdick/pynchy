"""Typed capability and host-action contracts.

Descriptors join plugin registration, semantic action evidence, policy metadata,
and operator diagnostics.  They describe what the host can attempt; dispatch-time
security policy remains authoritative.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NewType

from pynchy.action_intents import ActionIntent
from pynchy.actions.api import ActionId, ActionSpec, ActionTransport
from pynchy.identifiers import (
    CapabilityId,
)
from pynchy.workspace.api import (
    ServiceTrustConfig,
)

HostToolName = NewType("HostToolName", str)
HostActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")


class CapabilityKind(StrEnum):
    """Operator-visible capability categories."""

    HOST_ACTION = "host_action"
    AGENT_CORE = "agent_core"  # noqa: V107
    CHANNEL = "channel"  # noqa: V107
    MCP_SERVER = "mcp_server"  # noqa: V107
    SKILL = "skill"  # noqa: V107
    DEVICE = "device"  # noqa: V107


class CapabilityRequirementKind(StrEnum):
    """A prerequisite that can be shown without exposing secret values."""

    WORKSPACE_TOOL = "workspace_tool"
    CONFIG = "config"
    CREDENTIAL_REFERENCE = "credential_reference"
    HOST_BINARY = "host_binary"
    AGENT_CORE = "agent_core"  # noqa: V107
    RUNTIME = "runtime"  # noqa: V107
    PERMISSION = "permission"  # noqa: V107


class CapabilityStatus(StrEnum):
    """Resolved availability of one capability for one workspace."""

    READY = "ready"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"
    DENIED_BY_POLICY = "denied_by_policy"
    DEGRADED = "degraded"
    NOT_ESTABLISHED = "not_established"


class ProbeStatus(StrEnum):
    """Bounded provider probe outcomes before workspace policy resolution."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class HostActionAccess(StrEnum):
    """Whether a host action reads provider state or can mutate it."""

    READ = "read"
    WRITE = "write"


class ApprovalMode(StrEnum):
    """Approval mechanism used when dispatch policy requires a person."""

    EXACT_REQUEST = "exact_request"
    SESSION_TOOL = "session_tool"


class ApprovalTrigger(StrEnum):
    """Policy inputs that can require human approval for a host action."""

    SERVICE_POLICY = "service_policy"
    CAPABILITY_ONLY = "capability_only"
    ALWAYS = "always"


class IdempotencyMode(StrEnum):
    """Replay protection owned by the host action."""

    NOT_REQUIRED = "not_required"
    IPC_REQUEST_ID = "ipc_request_id"


class AuditMode(StrEnum):
    """Audit sink used for policy and terminal execution outcomes."""

    SECURITY_EVENT = "security_event"


@dataclass(frozen=True)
class CapabilityRequirement:
    """One safe-to-display prerequisite for a capability."""

    kind: CapabilityRequirementKind
    name: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "description": self.description,
        }


@dataclass(frozen=True)
class CapabilityProbeContext:
    """Workspace identity passed to a read-only availability probe."""

    workspace: str


@dataclass(frozen=True)
class CapabilityProbeResult:
    """Safe result from a bounded read-only availability probe."""

    status: ProbeStatus
    reason: str | None = None


CapabilityProbe = Callable[[CapabilityProbeContext], Awaitable[CapabilityProbeResult]]


@dataclass(frozen=True)
class ActionIntentDraft:
    """Validated provider-neutral write payload before an external call begins."""

    recipient: str
    payload: dict[str, Any]
    summary: str


@dataclass(frozen=True)
class ActionIntentReceipt:
    """Provider evidence proving that one external write completed."""

    provider_request_id: str
    receipt: dict[str, Any]


ActionIntentDraftFactory = Callable[[dict[str, Any]], ActionIntentDraft]
ActionIntentReceiptParser = Callable[[dict[str, Any]], ActionIntentReceipt]
ActionIntentExecutionDataFactory = Callable[[dict[str, Any], str], dict[str, Any]]
ActionIntentUnknownReconciler = Callable[[ActionIntent], Awaitable[ActionIntentReceipt | None]]


@dataclass(frozen=True)
class ActionIntentContract:
    """Provider-specific parsing boundary for a durable external write."""

    provider: str
    draft_from_request: ActionIntentDraftFactory = field(compare=False, repr=False)
    receipt_from_response: ActionIntentReceiptParser = field(compare=False, repr=False)
    execution_data_from_request: ActionIntentExecutionDataFactory | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    reconcile_unknown: ActionIntentUnknownReconciler | None = field(
        default=None,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class CapabilityDescriptor:
    """Immutable plugin-owned description of one user-meaningful capability."""

    id: CapabilityId
    kind: CapabilityKind
    owner: str
    summary: str
    action_ids: tuple[ActionId, ...]
    requirements: tuple[CapabilityRequirement, ...] = ()
    setup_hint: str | None = None
    recovery_hint: str | None = None
    documentation: str | None = None
    probe: CapabilityProbe | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "kind": self.kind.value,
            "owner": self.owner,
            "summary": self.summary,
            "action_ids": [str(action_id) for action_id in self.action_ids],
            "requirements": [requirement.to_dict() for requirement in self.requirements],
            "setup_hint": self.setup_hint,
            "recovery_hint": self.recovery_hint,
            "documentation": self.documentation,
        }


@dataclass(frozen=True)
class ApprovalContract:
    """How the existing approval state machine holds and replays an action."""

    mode: ApprovalMode = ApprovalMode.EXACT_REQUEST
    trigger: ApprovalTrigger = ApprovalTrigger.SERVICE_POLICY
    expires_after_seconds: int = 300


@dataclass(frozen=True)
class IdempotencyContract:
    """How duplicate action requests are prevented from executing twice."""

    mode: IdempotencyMode


@dataclass(frozen=True)
class AuditContract:
    """Evidence an action must emit during policy and execution transitions."""

    mode: AuditMode = AuditMode.SECURITY_EVENT
    terminal_outcomes: bool = True


@dataclass(frozen=True)
class HostActionDescriptor:
    """Typed host handler plus its security and evidence contracts."""

    capability: CapabilityDescriptor
    tool_name: HostToolName
    handler: HostActionHandler = field(compare=False, repr=False)
    access: HostActionAccess
    approval: ApprovalContract
    idempotency: IdempotencyContract
    audit: AuditContract
    policy_service: str | None = None
    default_service_trust: ServiceTrustConfig | None = None
    action_intent: ActionIntentContract | None = None

    @property
    def service_name(self) -> str:
        """Return the configured service-trust key for dispatch policy."""
        return self.policy_service or str(self.tool_name)


@dataclass(frozen=True)
class HostActionRegistration:
    """One plugin's immutable host-action contribution."""

    actions: tuple[HostActionDescriptor, ...]

    def action_for(self, tool_name: str) -> HostActionDescriptor | None:
        return next(
            (action for action in self.actions if action.tool_name == tool_name),
            None,
        )


def missing_workspace_tool(
    action: HostActionDescriptor,
    enabled_tools: Iterable[str],
    *,
    service_aliases: Iterable[str] = (),
) -> str | None:
    """Return the first prerequisite omitted from tools or stable service aliases."""
    enabled = frozenset(enabled_tools) | frozenset(service_aliases)
    required = tuple(
        requirement.name
        for requirement in action.capability.requirements
        if requirement.kind is CapabilityRequirementKind.WORKSPACE_TOOL
    ) or (str(action.tool_name),)
    return next((tool for tool in required if tool not in enabled), None)


@dataclass(frozen=True)
class ResolvedCapability:
    """Workspace-specific descriptor result for status and planning."""

    descriptor: CapabilityDescriptor
    status: CapabilityStatus
    reason: str | None = None
    approval_required: bool = False
    cop_review_required: bool = False
    canary_scenarios: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            **self.descriptor.to_dict(),
            "status": self.status.value,
            "reason": self.reason,
            "approval_required": self.approval_required,
            "cop_review_required": self.cop_review_required,
            "canary_scenarios": list(self.canary_scenarios),
        }


@dataclass(frozen=True)
class WorkspaceCapabilitySnapshot:
    """Immutable capability truth for one workspace at one point in time."""

    workspace: str
    generated_at: str
    capabilities: tuple[ResolvedCapability, ...]

    def to_dict(self) -> dict[str, object]:
        counts = {status.value: 0 for status in CapabilityStatus}
        for capability in self.capabilities:
            counts[capability.status.value] += 1
        return {
            "workspace": self.workspace,
            "generated_at": self.generated_at,
            "summary": counts,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
        }


class CapabilityCatalogError(ValueError):
    """Raised when host-action declarations cannot form a safe catalog."""


def validate_host_action_descriptors(
    descriptors: Iterable[HostActionDescriptor],
    action_specs: Iterable[ActionSpec],
) -> tuple[str, ...]:
    """Return every structural and semantic catalog error."""
    actions = tuple(descriptors)
    specs = {str(spec.id): spec for spec in action_specs}
    errors: list[str] = []
    seen_capabilities: set[str] = set()
    seen_tools: set[str] = set()

    for action in actions:
        capability_id = str(action.capability.id)
        tool_name = str(action.tool_name)
        if not _IDENTIFIER_RE.fullmatch(capability_id):
            errors.append(f"invalid capability id: {capability_id!r}")
        if capability_id in seen_capabilities:
            errors.append(f"duplicate capability id: {capability_id}")
        seen_capabilities.add(capability_id)
        if not tool_name.strip():
            errors.append(f"{capability_id}: tool name is required")
        if tool_name in seen_tools:
            errors.append(f"duplicate host tool name: {tool_name}")
        seen_tools.add(tool_name)
        errors.extend(_descriptor_errors(action, specs))
    return tuple(errors)


def _descriptor_errors(
    action: HostActionDescriptor,
    specs: dict[str, ActionSpec],
) -> list[str]:
    capability = action.capability
    capability_id = str(capability.id)
    errors: list[str] = []
    required_values = (
        (callable(action.handler), "handler must be callable"),
        (bool(capability.owner.strip()), "owner is required"),
        (bool(capability.summary.strip()), "summary is required"),
        (bool(capability.action_ids), "at least one ActionSpec is required"),
        (action.approval.expires_after_seconds > 0, "approval expiry must be positive"),
    )
    errors.extend(f"{capability_id}: {message}" for valid, message in required_values if not valid)
    if action.access is HostActionAccess.WRITE:
        if action.idempotency.mode is IdempotencyMode.NOT_REQUIRED:
            errors.append(f"{capability_id}: write action requires idempotency")
        if not action.audit.terminal_outcomes:
            errors.append(f"{capability_id}: write action requires terminal audit outcomes")
    if action.action_intent is not None:
        if action.access is not HostActionAccess.WRITE:
            errors.append(f"{capability_id}: action intent requires write access")
        if not action.action_intent.provider.strip():
            errors.append(f"{capability_id}: action intent provider is required")
    errors.extend(_action_spec_errors(action, specs))
    return errors


def _action_spec_errors(
    action: HostActionDescriptor,
    specs: dict[str, ActionSpec],
) -> list[str]:
    capability_id = str(action.capability.id)
    tool_name = str(action.tool_name)
    errors: list[str] = []
    for action_id in action.capability.action_ids:
        spec = specs.get(str(action_id))
        if spec is None:
            errors.append(f"{capability_id}: unknown ActionSpec {action_id}")
        elif not any(
            surface.transport is ActionTransport.AGENT_TOOL
            and _surface_matches_tool(surface.name, tool_name)
            for surface in spec.surfaces
        ):
            errors.append(
                f"{capability_id}: ActionSpec {action_id} does not expose tool {tool_name}"
            )
    return errors


def _surface_matches_tool(surface_name: str, tool_name: str) -> bool:
    """Match exact names plus ActionSpec placeholders such as ``{profile}``."""
    escaped = re.escape(surface_name)
    pattern = re.sub(r"\\\{[a-zA-Z_][a-zA-Z0-9_]*\\\}", r"[^.]+", escaped)
    return re.fullmatch(pattern, tool_name) is not None
