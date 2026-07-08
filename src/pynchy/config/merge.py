"""Three-tier workspace profile config merge: universal < profile < workspace.

Produces a fully-resolved config by cascading ``universal``, an optional
``profiles.<name>``, and the per-workspace :class:`WorkspaceConfig`.

Union fields (directives, skills, mcp_servers) are merged across all tiers
with order-preserved deduplication.  Override fields use most-specific-wins
semantics, checked via ``model_fields_set``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pynchy.config.models import (
    CapabilityTomlConfig,
    ProfileConfig,
    WorkspaceConfig,
    WorkspaceSecurityTomlConfig,
)
from pynchy.logger import logger

# Hardcoded defaults for override fields when no tier sets them.
_HARDCODED_DEFAULTS: dict[str, Any] = {
    "context_mode": "group",
    "access": "readwrite",
    "mode": "agent",
    "trust": True,
    "trigger": "mention",
    "allowed_users": ["owner"],
    "idle_terminate": True,
    "git_policy": "merge-to-main",
    "security": None,
    "repo_access": None,
    "model": None,
    "fallback_model": None,
    "is_admin": False,
    "contains_secrets": False,
}

# Fields that use union (list-merge) semantics.
_UNION_FIELDS = ("tags", "directives", "skills", "mcp_servers")

# Fields that use override (most-specific-wins) semantics.
_OVERRIDE_FIELDS = tuple(_HARDCODED_DEFAULTS.keys())


@dataclass(frozen=True)
class ResolvedSandboxConfig:
    """Fully-resolved workspace config after three-tier merge."""

    # Union fields
    directives: list[str]
    skills: list[str]
    mcp_servers: list[str]
    tags: list[str]
    capabilities: dict[str, CapabilityTomlConfig]

    # Override fields
    context_mode: str
    access: str
    mode: str
    trust: bool
    trigger: str
    allowed_users: list[str]
    idle_terminate: bool
    git_policy: str
    security: WorkspaceSecurityTomlConfig | None
    repo_access: str | None
    model: str | None
    fallback_model: str | None
    is_admin: bool
    contains_secrets: bool

    # Pass-through from WorkspaceConfig (not overridable)
    chat: str | None
    schedule: str | None
    prompt: str | None
    name: str | None
    mcp: dict[str, dict[str, Any]]


def _deduplicate(items: list[str]) -> list[str]:
    """Order-preserved deduplication."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _resolve_union(
    field: str,
    universal: ProfileConfig | None,
    profile: ProfileConfig | None,
    sandbox: WorkspaceConfig,
) -> list[str]:
    """Merge a union field across three tiers with deduplication."""
    parts: list[str] = []
    for tier, label in (
        (universal, "universal"),
        (profile, "profile"),
        (sandbox, "workspace"),
    ):
        if tier is None:
            continue
        value = getattr(tier, field, None)
        if value is not None:
            logger.debug("merge.union", field=field, tier=label, values=value)
            parts.extend(value)
        else:
            logger.debug("merge.union", field=field, tier=label, values="(none)")

    result = _deduplicate(parts)
    logger.debug("merge.union.resolved", field=field, result=result)
    return result


def _resolve_capabilities(
    universal: ProfileConfig | None,
    profile: ProfileConfig | None,
    sandbox: WorkspaceConfig,
) -> dict[str, CapabilityTomlConfig]:
    """Merge capability maps with most-specific values winning per key."""
    result: dict[str, CapabilityTomlConfig] = {}
    for tier, label in (
        (universal, "universal"),
        (profile, "profile"),
        (sandbox, "workspace"),
    ):
        if tier is None:
            continue
        capabilities = getattr(tier, "capabilities", None)
        if capabilities is None:
            logger.debug("merge.capabilities", tier=label, values="(none)")
            continue
        logger.debug("merge.capabilities", tier=label, values=list(capabilities))
        result.update(capabilities)

    logger.debug("merge.capabilities.resolved", result=list(result))
    return result


def _resolve_override(
    field: str,
    universal: ProfileConfig | None,
    profile: ProfileConfig | None,
    sandbox: WorkspaceConfig,
) -> Any:
    """Resolve an override field: most-specific explicitly-set value wins."""
    # Walk from most-specific to least-specific.
    tiers: list[tuple[ProfileConfig | WorkspaceConfig | None, str]] = [
        (sandbox, "workspace"),
        (profile, "profile"),
        (universal, "universal"),
    ]

    winner_value = None
    winner_label = None

    for tier, label in tiers:
        if tier is None:
            continue
        if field in tier.model_fields_set:
            value = getattr(tier, field)
            if winner_label is None:
                winner_value = value
                winner_label = label
                logger.debug(
                    "merge.override",
                    field=field,
                    tier=label,
                    value=value,
                    status="winner",
                )
            else:
                # A lower-priority tier also set this field -- log the conflict.
                logger.info(
                    "merge.override.shadowed",
                    field=field,
                    shadowed_tier=label,
                    shadowed_value=value,
                    winner_tier=winner_label,
                    winner_value=winner_value,
                )
        else:
            logger.debug(
                "merge.override",
                field=field,
                tier=label,
                status="not_set",
            )

    if winner_label is not None:
        return winner_value

    # No tier set it -- use hardcoded default.
    default = _HARDCODED_DEFAULTS[field]
    logger.debug("merge.override.default", field=field, value=default)
    return default


def merge_sandbox_config(
    universal: ProfileConfig | None,
    profile: ProfileConfig | None,
    sandbox: WorkspaceConfig,
) -> ResolvedSandboxConfig:
    """Cascade universal < profile < workspace config.

    Parameters
    ----------
    universal:
        The ``[universal]`` section, or ``None`` if absent.
    profile:
        The resolved ``profiles.<name>`` section, or ``None`` if no profile is
        referenced.
    sandbox:
        The per-workspace ``WorkspaceConfig``.

    Returns
    -------
    ResolvedSandboxConfig
        Frozen dataclass with every field resolved.
    """
    # Union fields
    union_results: dict[str, list[str]] = {}
    for field in _UNION_FIELDS:
        union_results[field] = _resolve_union(field, universal, profile, sandbox)
    capabilities = _resolve_capabilities(universal, profile, sandbox)

    # Override fields
    override_results: dict[str, Any] = {}
    for field in _OVERRIDE_FIELDS:
        override_results[field] = _resolve_override(field, universal, profile, sandbox)

    return ResolvedSandboxConfig(
        # Union
        directives=union_results["directives"],
        skills=union_results["skills"],
        mcp_servers=union_results["mcp_servers"],
        tags=union_results["tags"],
        capabilities=capabilities,
        # Override
        context_mode=override_results["context_mode"],
        access=override_results["access"],
        mode=override_results["mode"],
        trust=override_results["trust"],
        trigger=override_results["trigger"],
        allowed_users=override_results["allowed_users"],
        idle_terminate=override_results["idle_terminate"],
        git_policy=override_results["git_policy"],
        security=override_results["security"],
        repo_access=override_results["repo_access"],
        model=override_results["model"],
        fallback_model=override_results["fallback_model"],
        is_admin=override_results["is_admin"],
        contains_secrets=override_results["contains_secrets"],
        # Pass-through
        chat=sandbox.chat,
        schedule=sandbox.schedule,
        prompt=sandbox.prompt,
        name=sandbox.name,
        mcp=sandbox.mcp,
    )
