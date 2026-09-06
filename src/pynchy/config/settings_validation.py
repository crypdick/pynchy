"""Cross-field validation helpers for the root settings model."""

from __future__ import annotations

from collections.abc import (
    Callable,
)
from typing import TYPE_CHECKING, Protocol, cast

from pynchy.actions.api import ACTION_SPECS
from pynchy.config.jobs import (
    JobConfig,
)
from pynchy.config.models import (
    AgentConfig,
    ToolConfig,
    WorkspaceConfig,
)
from pynchy.config.profiles import (
    ProfileConfig,
)
from pynchy.config.refs import ChatRef, parse_chat_ref
from pynchy.config.scheduler_models import (
    CanaryConfig,
)
from pynchy.config.workspace_layout import semantic_workspace_configs
from pynchy.discord import discord_chat_ref_error
from pynchy.security_canary_ids import SECURITY_CANARY_IDS

if TYPE_CHECKING:
    from pynchy.config.settings import Settings
else:
    Settings = object


class _CanaryValidationSettings(Protocol):
    """Settings fields needed to validate selected external-service canaries."""

    canary: CanaryConfig
    profiles: dict[str, ProfileConfig]
    workspaces: dict[str, WorkspaceConfig]


def validate_profile_references(
    *,
    profiles: dict[str, ProfileConfig],
    workspaces: dict[str, WorkspaceConfig],
    jobs: dict[str, JobConfig],
    tools: dict[str, ToolConfig],
    expand_profile_names: Callable[[str], list[str]],
) -> None:
    """Ensure workspace profiles, job workspaces, and profile tools resolve."""
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
    semantic = semantic_workspace_configs(workspaces)
    for folder, (_parent, thread) in semantic.items():
        for profile_name in thread.profiles:
            if profile_name not in profiles:
                message = (
                    f"workspaces.{folder}.profiles references unknown profile: "
                    f"'{profile_name}'. Available: {list(profiles.keys())}"
                )
                raise ValueError(message)
    workspace_names = {*workspaces, *semantic}
    for job_name, job in jobs.items():
        _validate_job_reference(job_name, job, workspace_names)


def _validate_job_reference(
    job_name: str,
    job: JobConfig,
    workspace_names: set[str],
) -> None:
    """Validate one config job's explicit parent workspace binding."""
    if job.is_host:
        return
    if job.workspace not in workspace_names:
        raise ValueError(f"jobs.{job_name}.workspace references unknown workspace: {job.workspace}")


def validate_workspace_chat_references(settings: Settings) -> None:
    """Ensure workspace chat bindings name configured connection targets."""
    for workspace_name, workspace in settings.workspaces.items():
        if workspace.chat is None:
            continue
        parsed = cast("ChatRef", parse_chat_ref(workspace.chat))
        connection = settings.connections.get(parsed.name)
        if connection is None:
            raise ValueError(
                f"workspaces.{workspace_name}.chat references unknown connection: {parsed.name}"
            )
        if parsed.platform != "discord":
            continue
        if connection.type != "discord":
            raise TypeError(
                f"workspaces.{workspace_name}.chat requires a Discord connection: {parsed.name}"
            )
        error = discord_chat_ref_error(connection.to_runtime_settings(), parsed.chat)
        if error is not None:
            raise ValueError(f"workspaces.{workspace_name}.chat {error}")


def validate_route_references(settings: Settings) -> None:
    """Ensure each route targets an existing endpoint and parent workspace."""
    used_sources: set[str] = set()
    for route_name, route in settings.routes.items():
        parsed = cast("ChatRef", parse_chat_ref(route.source))
        connection = settings.connections.get(parsed.name)
        if connection is None:
            raise ValueError(f"routes.{route_name}.source references an unknown connection")
        if connection.type != parsed.platform:
            raise TypeError(f"routes.{route_name}.source platform does not match its connection")
        endpoint = connection.chat.get(parsed.chat)
        if endpoint is None:
            raise ValueError(f"routes.{route_name}.source references an unknown endpoint")
        if getattr(endpoint, "enabled", True) is not True:
            raise ValueError(f"routes.{route_name}.source references a disabled endpoint")
        if settings.workspace_config(route.workspace) is None:
            raise ValueError(f"routes.{route_name}.workspace references an unknown workspace")
        if route.source in used_sources:
            raise ValueError("One endpoint cannot map to multiple enabled routes")
        used_sources.add(route.source)


def validate_canary_target_profile(
    settings: _CanaryValidationSettings,
    expand_profile_names: Callable[[str], list[str]],
) -> None:
    """Ensure an enabled canary schedule has safe, configured service targets."""
    canary = settings.canary
    if not canary.enabled:
        return
    if canary.target_profile not in settings.profiles:
        message = f"canary.target_profile references unknown profile: {canary.target_profile}"
        raise ValueError(message)
    if not canary.scenario_ids:
        raise ValueError("canary.scenario_ids is required when canaries are enabled")

    declared = {
        *(spec.canary_scenario for spec in ACTION_SPECS if spec.canary_scenario is not None),
        *SECURITY_CANARY_IDS,
    }
    unknown = sorted(set(canary.scenario_ids) - declared)
    if unknown:
        raise ValueError(f"canary.scenario_ids includes unknown scenarios: {unknown}")

    resolved_tools = {
        str(tool_name)
        for profile_name in expand_profile_names(canary.target_profile)
        for tool_name in settings.profiles[profile_name].tools
    }
    _validate_canary_tools(canary.scenario_ids, resolved_tools)
    _validate_canary_target_values(canary, settings.workspaces)


def _validate_canary_tools(scenario_ids: list[str], resolved_tools: set[str]) -> None:
    """Ensure each selected scenario has its declared integration enabled."""
    # NOTE: Update docs/architecture/action-coverage.md § Agentic canary runner.
    required_tools = {
        "calendar.round.trip": "caldav",
        "desktop.computer.round.trip": "computer_use",
        "calendar.google.round.trip": "gcal",
        "drive.google.round.trip": "gdrive",
        "linear.workspace.round.trip": "linear",
        "proton.mail.round.trip": "proton-mail",
    }
    missing_tools = sorted(
        required_tool
        for scenario_id, required_tool in required_tools.items()
        if scenario_id in scenario_ids and required_tool not in resolved_tools
    )
    if missing_tools:
        raise ValueError(
            "canary.target_profile does not enable required tools: " + ", ".join(missing_tools)
        )


def _validate_canary_target_values(
    canary: CanaryConfig,
    workspaces: dict[str, WorkspaceConfig],
) -> None:
    """Require dedicated non-secret targets for each selected canary scenario."""
    requirements = {
        "calendar.round.trip": (("calendar_name", "calendar_name"),),
        "calendar.google.round.trip": (
            ("google_calendar_server", "google_calendar_server"),
            ("google_calendar_id", "google_calendar_id"),
        ),
        "drive.google.round.trip": (
            ("google_drive_server", "google_drive_server"),
            ("google_drive_probe_query", "google_drive_probe_query"),
            ("google_drive_file_id", "google_drive_file_id"),
        ),
        "linear.workspace.round.trip": (
            ("linear_team_key", "linear_team_key"),
            ("linear_workspace", "linear_workspace"),
        ),
        "proton.mail.round.trip": (("proton_recipient", "proton_recipient"),),
    }
    for scenario_id in canary.scenario_ids:
        for field_name, display_name in requirements.get(scenario_id, ()):
            value = getattr(canary, field_name)
            if not value.strip():
                raise ValueError(f"canary.{display_name} is required for {scenario_id}")
    if "linear.workspace.round.trip" in canary.scenario_ids and canary.linear_workspace not in {
        *workspaces,
        *semantic_workspace_configs(workspaces),
    }:
        raise ValueError(
            f"canary.linear_workspace references unknown workspace: {canary.linear_workspace}"
        )


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
    override_paths.extend(
        f"workspaces.{workspace_name}.model"
        for workspace_name, (_parent, thread) in semantic_workspace_configs(workspaces).items()
        if thread.model is not None
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
