"""Composable profile resolution for workspace config."""

from __future__ import annotations

from dataclasses import dataclass

from pynchy.config.models import ProfileConfig


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

    prompts: list[str]
    skills: list[str]
    tools: list[str]
    repo: list[str]
    model: str | None
    is_admin: bool
    contains_secrets: bool


def merge_workspace_profiles(profiles: list[ProfileConfig]) -> ResolvedWorkspaceConfig:
    """Merge profiles in already-expanded composition order."""
    prompts: list[str] = []
    skills: list[str] = []
    tools: list[str] = []
    repo: list[str] = []
    model: str | None = None
    is_admin = False
    contains_secrets = False

    for profile in profiles:
        prompts.extend(profile.prompts)
        skills.extend(profile.skills)
        tools.extend(profile.tools)
        repo.extend(profile.repo)
        if profile.model is not None:
            model = profile.model
        is_admin = is_admin or profile.is_admin
        contains_secrets = contains_secrets or profile.contains_secrets

    return ResolvedWorkspaceConfig(
        prompts=_deduplicate(prompts),
        skills=_deduplicate(skills),
        tools=_deduplicate(tools),
        repo=_deduplicate(repo),
        model=model,
        is_admin=is_admin,
        contains_secrets=contains_secrets,
    )
