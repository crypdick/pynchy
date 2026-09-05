"""Semantic contracts for one configured Pynchy workspace."""

from __future__ import annotations

from collections.abc import (
    Iterable,  # noqa: TC003 - beartype resolves policy annotations at runtime.
)
from dataclasses import dataclass, field
from typing import Any, Literal

from pynchy.identifiers import ChatJid, GroupFolder, RuntimeId

APPROVAL_TIMEOUT_SECONDS = 300

_CONTAINER_TIMEOUT_ERROR = "container_config.timeout: expected number, got {type_name}"
_CONTAINER_MOUNTS_ERROR = "container_config.additional_mounts: expected list, got {type_name}"
_CAPABILITY_DECISION_RANK = {"allow": 0, "needs_human": 1, "deny": 2}

TrustLevel = Literal["forbidden"] | bool
CapabilityDecision = Literal["allow", "deny", "needs_human"]


@dataclass
class AdditionalMount:
    host_path: str
    container_path: str | None = None
    readonly: bool = True


@dataclass
class AllowedRoot:
    path: str
    allow_read_write: bool = False
    description: str | None = None


@dataclass
class MountAllowlist:
    allowed_roots: list[AllowedRoot] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    non_admin_read_only: bool = True


@dataclass
class ContainerConfig:
    additional_mounts: list[AdditionalMount] = field(default_factory=list)
    timeout: int | float | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ContainerConfig:
        timeout = raw.get("timeout")
        if timeout is not None and not isinstance(timeout, int | float):
            raise TypeError(_CONTAINER_TIMEOUT_ERROR.format(type_name=type(timeout).__name__))
        mounts = raw.get("additional_mounts", [])
        if not isinstance(mounts, list):
            raise TypeError(_CONTAINER_MOUNTS_ERROR.format(type_name=type(mounts).__name__))
        return cls(
            additional_mounts=[AdditionalMount(**mount) for mount in mounts],
            timeout=timeout,
        )


@dataclass
class CapabilityRule:
    """Explicit policy for a semantic capability such as an MCP tool call."""

    decision: CapabilityDecision


def capability_pattern_matches(pattern: str, capability: str) -> bool:
    """Return whether an exact capability is covered by a trailing wildcard."""
    if pattern == "*":
        return True
    if pattern == capability:
        return True
    if not pattern.endswith(".*"):
        return False
    prefix = pattern[:-2]
    return bool(prefix) and capability.startswith(f"{prefix}.")


def most_restrictive_capability_rule(
    rules: Iterable[CapabilityRule],
) -> CapabilityRule | None:
    """Intersect matching rules by selecting the most restrictive decision."""
    return max(rules, key=lambda rule: _CAPABILITY_DECISION_RANK[rule.decision], default=None)


@dataclass(frozen=True)
class ServiceTrustConfig:
    """Four user-facing trust properties for one external service."""

    public_source: TrustLevel = True
    secret_data: bool = True
    public_sink: TrustLevel = True
    dangerous_writes: TrustLevel = True


@dataclass
class WorkspaceSecurity:
    """Security configuration and service policy for a workspace."""

    services: dict[str, ServiceTrustConfig] = field(default_factory=dict)
    contains_secrets: bool = False
    cop_active: bool = True
    capabilities: dict[str, CapabilityRule] = field(default_factory=dict)


@dataclass
class WorkspaceProfile:
    """Complete runtime configuration and security profile for one workspace."""

    jid: str
    name: str
    folder: str
    trigger: str
    container_config: ContainerConfig | None = None
    security: WorkspaceSecurity = field(default_factory=WorkspaceSecurity)
    is_admin: bool = False
    added_at: str = ""

    def validate(self) -> list[str]:
        errors = []
        if not self.name:
            errors.append("Workspace name is required")
        if not self.folder:
            errors.append("Workspace folder is required")
        if not self.trigger:
            errors.append("Workspace trigger is required")
        return errors


@dataclass(frozen=True, slots=True)
class RuntimeTarget:
    """One execution runtime and its human-facing control address."""

    folder: GroupFolder
    chat_jid: ChatJid

    @property
    def id(self) -> RuntimeId:
        return RuntimeId(self.folder)

    @classmethod
    def from_workspace(cls, workspace: WorkspaceProfile) -> RuntimeTarget:
        return cls.from_binding(workspace.folder, workspace.jid)

    @classmethod
    def from_binding(cls, folder: str, chat_jid: str) -> RuntimeTarget:
        return cls(folder=GroupFolder(folder), chat_jid=ChatJid(chat_jid))


@dataclass(frozen=True)
class ResolvedWorkspaceConfig:
    """Fully resolved config after expanding and merging selected profiles."""

    skills: list[str]
    tools: list[str]
    repo: list[str]
    model: str | None
    execution_mode: str
    cwd: str | None
    is_admin: bool
    contains_secrets: bool
    model_reasoning_effort: str | None = None
    cop_active: bool = True
    soul: str | None = None
    pipeline: str | None = None
    denied_skills: list[str] = field(default_factory=list)
    capabilities: dict[str, CapabilityRule] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolvedToolAccess:
    """One workspace's available tool grants without credential values in metadata."""

    tools: tuple[str, ...]
    companion_skills: tuple[str, ...]
    workspace_env: dict[str, str]
    missing_requirements: dict[str, tuple[str, ...]]
    agent_tool_grants: tuple[str, ...] = ()

    @property
    def notices(self) -> tuple[str, ...]:
        return tuple(
            f"Tool {name!r} is unavailable; missing required environment: "
            + ", ".join(requirements)
            for name, requirements in self.missing_requirements.items()
        )
