"""Resolve tool authorization, availability, skills, and process environments."""

from __future__ import annotations

import os
from collections.abc import (  # noqa: TC003 - beartype resolves tool-access annotations at runtime.
    Mapping,
)
from dataclasses import replace

from pynchy.config.models import (
    BuiltinTool,
    CalDAVTool,
    LinearTool,
    ToolConfig,
    WorkspaceTool,
)
from pynchy.workspace.api import ResolvedToolAccess, ResolvedWorkspaceConfig


def resolve_tool_access(
    configured_tools: Mapping[str, ToolConfig],
    resolved: ResolvedWorkspaceConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedToolAccess:
    """Apply the TOML grant and current requirement availability once."""
    source = os.environ if environ is None else environ
    tools: list[str] = []
    skills: list[str] = []
    workspace_env: dict[str, str] = {}
    missing: dict[str, tuple[str, ...]] = {}
    agent_tool_grants: list[str] = []

    for name in resolved.tools:
        tool = configured_tools.get(name)
        if tool is None:
            # A runtime hook can implement a tool, but only a TOML tool
            # declaration can authorize it.
            continue
        if not tool.enabled:
            continue
        absent = tuple(env_name for env_name in tool.required_env if not source.get(env_name))
        if absent:
            missing[name] = absent
            continue
        tools.append(name)
        agent_tool_grants.extend(_agent_tool_grants(name, tool))
        skills.extend(tool.skills)
        if isinstance(tool, WorkspaceTool) or tool.expose_env_to_workspace:
            workspace_env.update(_declared_environment(tool, source))

    return ResolvedToolAccess(
        tools=tuple(tools),
        companion_skills=tuple(dict.fromkeys(skills)),
        workspace_env=workspace_env,
        missing_requirements=missing,
        agent_tool_grants=tuple(dict.fromkeys(agent_tool_grants)),
    )


def _agent_tool_grants(name: str, tool: ToolConfig) -> tuple[str, ...]:
    """Return stable aliases that expose matching built-in agent tools."""
    if isinstance(tool, BuiltinTool):
        return (name, tool.name) if tool.name and tool.name != name else (name,)
    if isinstance(tool, (LinearTool, CalDAVTool)):
        return (name, tool.type)
    return (name,)


def apply_tool_access(
    configured_tools: Mapping[str, ToolConfig],
    resolved: ResolvedWorkspaceConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[ResolvedWorkspaceConfig, ResolvedToolAccess]:
    """Return runtime workspace policy plus its value-free access report."""
    access = resolve_tool_access(configured_tools, resolved, environ=environ)
    all_companion_skills = {
        skill for configured_tool in configured_tools.values() for skill in configured_tool.skills
    }
    ordinary_skills = [skill for skill in resolved.skills if skill not in all_companion_skills]
    skills = list(dict.fromkeys([*ordinary_skills, *access.companion_skills]))
    return (
        replace(
            resolved,
            tools=list(access.tools),
            skills=skills,
            contains_secrets=resolved.contains_secrets or bool(access.workspace_env),
        ),
        access,
    )


def tool_process_environment(
    tool: ToolConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve only variables declared by one available tool for its runtime."""
    source = os.environ if environ is None else environ
    values = _declared_environment(tool, source)
    if isinstance(tool, LinearTool):
        result = {"LINEAR_API_KEY": values[tool.api_key_env]}
        if tool.optional_env and (team_key := values.get(tool.team_key_env)) is not None:
            result["LINEAR_TEAM_KEY"] = team_key
        return result
    return values


def _declared_environment(
    tool: ToolConfig,
    source: Mapping[str, str],
) -> dict[str, str]:
    names = (*tool.required_env, *tool.optional_env)
    return {name: source[name] for name in names if source.get(name)}
