"""Composable profile resolution for workspace config."""

from __future__ import annotations

from dataclasses import dataclass, field

from pynchy.config.profiles import (
    ProfileConfig,  # noqa: TC001 - beartype resolves annotations at runtime.
)
from pynchy.workspace.api import CapabilityRule


def _deduplicate(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


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


def merge_workspace_profiles(profiles: list[ProfileConfig]) -> ResolvedWorkspaceConfig:
    """Merge profiles in already-expanded composition order."""
    skills: list[str] = []
    denied_skills: list[str] = []
    tools: list[str] = []
    repo: list[str] = []
    model: str | None = None
    execution_mode = "container"
    cwd: str | None = None
    is_admin = False
    contains_secrets = False
    cop_active = True
    capabilities: dict[str, CapabilityRule] = {}

    for profile in profiles:
        skills.extend(profile.skills)
        denied_skills.extend(profile.denied_skills)
        tools.extend(profile.tools)
        repo.extend(profile.repo)
        if profile.model is not None:
            model = profile.model
        if profile.execution_mode is not None:
            execution_mode = profile.execution_mode
        if profile.cwd is not None:
            cwd = profile.cwd
        is_admin = is_admin or profile.is_admin
        contains_secrets = contains_secrets or profile.contains_secrets
        if profile.cop_active is not None:
            cop_active = profile.cop_active
        capabilities.update(
            {
                capability: CapabilityRule(decision=decision)
                for capability, decision in profile.permission_decisions.items()
            }
        )

    return ResolvedWorkspaceConfig(
        skills=_deduplicate(skills),
        denied_skills=_deduplicate(denied_skills),
        tools=_deduplicate(tools),
        repo=_deduplicate(repo),
        model=model,
        model_reasoning_effort=None,
        execution_mode=execution_mode,
        cwd=cwd,
        is_admin=is_admin,
        contains_secrets=contains_secrets,
        cop_active=cop_active,
        capabilities=capabilities,
    )
