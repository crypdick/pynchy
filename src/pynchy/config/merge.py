"""Composable profile resolution for workspace config."""

from __future__ import annotations

from pynchy.config.profiles import (
    ProfileConfig,  # noqa: TC001 - beartype resolves annotations at runtime.
)
from pynchy.workspace.api import (
    CapabilityRule,
    ResolvedWorkspaceConfig,
    most_restrictive_capability_rule,
)


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
        for capability, decision in profile.permissions.decisions.items():
            rule = CapabilityRule(decision=decision)
            existing = capabilities.get(capability)
            capabilities[capability] = (
                most_restrictive_capability_rule((existing, rule)) if existing else rule
            ) or rule

    return ResolvedWorkspaceConfig(
        skills=list(dict.fromkeys(skills)),
        denied_skills=list(dict.fromkeys(denied_skills)),
        tools=list(dict.fromkeys(tools)),
        repo=list(dict.fromkeys(repo)),
        model=model,
        model_reasoning_effort=None,
        execution_mode=execution_mode,
        cwd=cwd,
        is_admin=is_admin,
        contains_secrets=contains_secrets,
        cop_active=cop_active,
        capabilities=capabilities,
    )
