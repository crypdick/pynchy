"""Cross-field validation helpers for the root settings model."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves annotations at runtime.
    Callable,
)

from pynchy.config.jobs import (  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
    JobConfig,
)
from pynchy.config.models import (  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
    AgentConfig,
    ToolConfig,
    WorkspaceConfig,
)
from pynchy.config.profiles import (  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
    ProfileConfig,
)


def validate_profile_references(
    *,
    profiles: dict[str, ProfileConfig],
    workspaces: dict[str, WorkspaceConfig],
    jobs: dict[str, JobConfig],
    tools: dict[str, ToolConfig],
    expand_profile_names: Callable[[str], list[str]],
) -> None:
    """Ensure workspace, job, and tool references resolve through profiles."""
    if "host" in workspaces:
        message = "'host' is reserved and cannot be a workspace name"
        raise ValueError(message)
    for profile_name in profiles:
        expand_profile_names(profile_name)
    for profile_name, profile in profiles.items():
        for tool_name in profile.tools:
            if tool_name not in tools:
                message = f"profiles.{profile_name}.tools references unknown tool: {tool_name}"
                raise ValueError(message)
    for folder, workspace in workspaces.items():
        for profile_name in workspace.profiles:
            if profile_name not in profiles:
                message = (
                    f"workspaces.{folder}.profiles references unknown profile: "
                    f"'{profile_name}'. Available: {list(profiles.keys())}"
                )
                raise ValueError(message)
    for job_name, job in jobs.items():
        if job.workspace != "host" and job.workspace not in workspaces:
            message = f"jobs.{job_name}.workspace references unknown workspace: {job.workspace}"
            raise ValueError(message)


def reject_claude_sdk_model_overrides(
    *,
    agent: AgentConfig,
    profiles: dict[str, ProfileConfig],
    workspaces: dict[str, WorkspaceConfig],
) -> None:
    """Reject model settings that the built-in Claude SDK core cannot honor."""
    if agent.default_core != "claude":
        return

    override_paths: list[str] = []
    if agent.model is not None:
        override_paths.append("agent.model")
    override_paths.extend(
        f"profiles.{profile_name}.model"
        for profile_name, profile in profiles.items()
        if profile.model is not None
    )
    override_paths.extend(
        f"workspaces.{workspace_name}.model"
        for workspace_name, workspace in workspaces.items()
        if workspace.model is not None
    )
    if not override_paths:
        return

    configured_paths = ", ".join(dict.fromkeys(override_paths))
    message = (
        "The Claude SDK core currently hard-codes its model to 'opus'; model overrides "
        "are not supported. Remove the configured model setting(s): "
        f"{configured_paths}. Use [agent].default_core = 'claude-cli' to select a model."
    )
    raise ValueError(message)
