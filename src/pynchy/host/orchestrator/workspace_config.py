"""Workspace configuration — reads layered personalization via Settings.

Workspaces are defined in workspaces/<name>.toml documents.
Runtime creation (e.g. via IPC) writes the corresponding workspace document.
"""

from __future__ import annotations

from collections.abc import (
    Awaitable,  # noqa: TC003 - beartype resolves workspace config annotations at runtime.
    Callable,  # noqa: TC003 - beartype resolves workspace config annotations at runtime.
    Iterable,  # noqa: TC003 - beartype resolves workspace config annotations at runtime.
)
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn, cast

import pluggy  # noqa: TC002 - beartype resolves plugin-manager annotations at runtime.
import tomlkit

from pynchy.atomic_json import write_text_atomic
from pynchy.conversation.api import conversation_id_from_folder, parent_workspace_name
from pynchy.conversation.api import dynamic_thread_folder as _dynamic_thread_folder
from pynchy.host.orchestrator.config_jobs import reconcile_agent_jobs
from pynchy.host.orchestrator.pipeline_context import (
    prompt_ids_for_context as _prompt_ids_for_context,
)
from pynchy.host.orchestrator.workspace_artifacts import (
    remove_orphaned_workspace_registrations,
)
from pynchy.host.orchestrator.workspace_registration import (
    ensure_workspace_registered,
    resolve_display_name,
    sync_workspace_profile,
)
from pynchy.host.orchestrator.workspace_threads import reconcile_workspace_threads
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,  # beartype resolves workspace config annotations at runtime.
    WorkspaceSpec,
)
from pynchy.state.api import get_all_tasks, update_task
from pynchy.workspace.api import (  # beartype resolves workspace config annotations at runtime.
    CapabilityRule,
    WorkspaceProfile,
    capability_pattern_matches,
    most_restrictive_capability_rule,
)

type JobConfig = Any
type ResolvedToolAccess = Any
type ResolvedWorkspaceConfig = Any
type Settings = Any
type WorkspaceConfig = Any


def _unconfigured_runtime(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("Workspace configuration has not been composed")


@dataclass(frozen=True)
class WorkspaceConfigRuntime:
    get_settings: Callable[[], Any]
    read_prompts: Callable[[list[str]], str | None]
    parse_workspace_config: Callable[[object], Any]
    apply_tool_access: Callable[..., tuple[Any, Any]]
    resolve_tool_access: Callable[..., Any]
    mutate_config_toml: Callable[..., object]
    validate_settings_mapping: Callable[[dict[str, Any]], Any]
    reset_settings: Callable[[], None]


_runtime = WorkspaceConfigRuntime(
    get_settings=_unconfigured_runtime,
    read_prompts=_unconfigured_runtime,
    parse_workspace_config=_unconfigured_runtime,
    apply_tool_access=_unconfigured_runtime,
    resolve_tool_access=_unconfigured_runtime,
    mutate_config_toml=_unconfigured_runtime,
    validate_settings_mapping=_unconfigured_runtime,
    reset_settings=_unconfigured_runtime,
)


def configure_workspace_config_runtime(runtime: WorkspaceConfigRuntime) -> None:
    """Bind configuration loading and persistence at host composition."""
    global _runtime  # noqa: PLW0603 - one host process owns workspace configuration.
    _runtime = runtime


def get_settings() -> Settings:
    return cast("Settings", _runtime.get_settings())


def read_prompts(names: list[str]) -> str | None:
    return _runtime.read_prompts(names)


def apply_tool_access(
    *args: object, **kwargs: object
) -> tuple[ResolvedWorkspaceConfig, ResolvedToolAccess]:
    return _runtime.apply_tool_access(*args, **kwargs)


def resolve_tool_access(*args: object, **kwargs: object) -> ResolvedToolAccess:
    return cast("ResolvedToolAccess", _runtime.resolve_tool_access(*args, **kwargs))


def mutate_config_toml(*args: object, **kwargs: object) -> None:
    _runtime.mutate_config_toml(*args, **kwargs)


def reset_settings() -> None:
    _runtime.reset_settings()


@dataclass
class _WorkspaceConfigState:
    plugin_workspace_specs: dict[str, WorkspaceSpec] = field(default_factory=dict)


_state = _WorkspaceConfigState()


@dataclass(frozen=True, slots=True)
class RuntimeWorkspacePolicy:
    """Connection-owned policy applied beneath one configured workspace."""

    parent_workspace: str
    tools: tuple[str, ...] | None = None
    capabilities: dict[str, CapabilityRule] = field(default_factory=dict)


_runtime_policies: dict[str, RuntimeWorkspacePolicy] = {}


def register_runtime_workspace_policy(
    folder: str,
    policy: RuntimeWorkspacePolicy,
) -> None:
    """Register runtime policy for one generated workspace."""
    _runtime_policies[folder] = policy


def ensure_runtime_workspace_policy_owner(
    folder: str,
    parent_workspace: str,
) -> None:
    """Install an owner mapping without replacing narrower route policy."""
    existing = _runtime_policies.get(folder)
    if existing is None:
        register_runtime_workspace_policy(
            folder,
            RuntimeWorkspacePolicy(parent_workspace=parent_workspace),
        )
        return
    if existing.parent_workspace != parent_workspace:
        raise ValueError("Runtime workspace policy has a different policy owner")


def clear_runtime_workspace_policies() -> None:
    """Clear connection-owned runtime policies during lifecycle teardown/tests."""
    _runtime_policies.clear()


def unregister_runtime_workspace_policy(folder: str) -> None:
    """Remove one generated workspace policy during connection teardown."""
    _runtime_policies.pop(folder, None)


def dynamic_thread_folder(parent_folder: str, thread_jid: str) -> str:
    """Return the isolated runtime folder for a dynamic thread workspace."""
    return _dynamic_thread_folder(parent_folder, thread_jid)


def _parent_folder_for_dynamic_thread(folder: str) -> str | None:
    return parent_workspace_name(folder)


def static_workspace_folder(folder: str) -> str:
    """Return the configured parent workspace folder for dynamic thread folders."""
    return _parent_folder_for_dynamic_thread(folder) or folder


def configure_plugin_workspaces(plugin_manager: pluggy.PluginManager | None) -> None:
    """Cache workspace specs exported by plugins.

    Plugin workspace configs are merged with layered settings in `load_workspace_config`.
    """
    _state.plugin_workspace_specs.clear()
    if plugin_manager is None:
        return

    for spec in plugin_manager.hook.pynchy_workspace_spec():
        if not isinstance(spec, WorkspaceSpec):
            logger.warning("Ignoring invalid workspace plugin spec", spec_type=type(spec).__name__)
            continue
        _state.plugin_workspace_specs[spec.folder] = spec


def _workspace_specs(settings: Settings | None = None) -> dict[str, WorkspaceSpec]:
    """Return merged workspace specs from plugins and personalization.

    User config always wins when both sources define the same workspace folder.
    """
    s = settings or get_settings()
    merged = dict(_state.plugin_workspace_specs)
    for folder, cfg in s.workspaces.items():
        merged[folder] = WorkspaceSpec(folder=folder, config=cfg.model_dump())
    return merged


def _workspace_spec_config(spec: WorkspaceSpec) -> WorkspaceConfig:
    """Validate a plugin's configuration transport at its application boundary."""
    return cast("WorkspaceConfig", _runtime.parse_workspace_config(spec.config))


def load_workspace_config(
    group_folder: str,
    *,
    settings: Settings | None = None,
) -> WorkspaceConfig | None:
    """Read workspace config for a group from Settings.

    Returns None if the group has no configured workspace document or plugin spec.
    """
    runtime_policy = _runtime_policies.get(group_folder)
    if conversation_id_from_folder(group_folder) is not None and runtime_policy is None:
        # A persisted route workspace can outlive its route configuration.
        # Never let that stale registration fall back to the full parent policy.
        return None
    effective_settings = settings or get_settings()
    specs = _workspace_specs(effective_settings)
    parent_folder = (
        runtime_policy.parent_workspace
        if runtime_policy is not None
        else _parent_folder_for_dynamic_thread(group_folder)
    )
    policy_folder = parent_folder or group_folder
    spec = specs.get(policy_folder)
    if spec is None:
        semantic = effective_settings.workspace_config(policy_folder)
        if semantic is not None:
            return semantic
    if spec is None:
        return None
    config = _workspace_spec_config(spec)

    logger.debug(
        "Loaded workspace config",
        folder=group_folder,
        profiles=list(config.profiles),
    )
    return config


def _load_declared_resolved_config(
    group_folder: str,
    *,
    settings: Settings | None = None,
) -> ResolvedWorkspaceConfig | None:
    """Load merged policy and apply runtime policy before credential access."""
    effective_settings = settings or get_settings()
    if load_workspace_config(group_folder, settings=effective_settings) is None:
        return None

    s = effective_settings
    runtime_policy = _runtime_policies.get(group_folder)
    resolved = s.resolved_workspace_config(group_folder)
    base: ResolvedWorkspaceConfig | None
    if resolved is not None:
        base = resolved
    else:
        parent_folder = (
            runtime_policy.parent_workspace
            if runtime_policy is not None
            else _parent_folder_for_dynamic_thread(group_folder)
        )
        base = s.resolved_workspace_config(parent_folder) if parent_folder is not None else None
    if base is None or runtime_policy is None:
        return base
    restricted_tools = (
        [tool for tool in base.tools if tool in runtime_policy.tools]
        if runtime_policy.tools is not None
        else list(base.tools)
    )
    capabilities = dict(base.capabilities)
    for capability, runtime_rule in runtime_policy.capabilities.items():
        inherited = tuple(
            rule
            for pattern, rule in base.capabilities.items()
            if capability_pattern_matches(pattern, capability)
        )
        inherited_rule = most_restrictive_capability_rule(inherited)
        effective = most_restrictive_capability_rule((*inherited, runtime_rule)) or runtime_rule
        if inherited_rule is not None and effective.decision != runtime_rule.decision:
            raise ValueError(
                f"Runtime workspace policy cannot widen explicit {capability!r} permission"
            )
        capabilities[capability] = effective
    return replace(base, tools=restricted_tools, capabilities=capabilities)


def load_resolved_tool_access(
    group_folder: str,
    *,
    settings: Settings | None = None,
) -> ResolvedToolAccess | None:
    """Return selected tool availability without exposing requirement values."""
    effective_settings = settings or get_settings()
    resolved = _load_declared_resolved_config(group_folder, settings=effective_settings)
    if resolved is None:
        return None
    return resolve_tool_access(effective_settings.tools, resolved)


def load_resolved_config(
    group_folder: str,
    *,
    settings: Settings | None = None,
) -> ResolvedWorkspaceConfig | None:
    """Load the effective workspace config after route and tool access policy."""
    effective_settings = settings or get_settings()
    resolved = _load_declared_resolved_config(group_folder, settings=effective_settings)
    if resolved is None:
        return None
    return apply_tool_access(effective_settings.tools, resolved)[0]


def prompt_ids_for_context(
    resolved: ResolvedWorkspaceConfig | None,
    input_source: str,
    *,
    settings: Settings | None = None,
) -> tuple[str, ...]:
    """Return the selected soul and role prompt for one agent context."""
    return _prompt_ids_for_context(
        resolved,
        input_source,
        settings=settings or get_settings(),
    )


def _first_repo(resolved: ResolvedWorkspaceConfig | None) -> str | None:
    if resolved is None:
        return None
    return resolved.repo[0] if resolved.repo else None


def get_repo_access(group_folder: str) -> str | None:
    """Return the first resolved repo slug for a group folder, if configured.

    Existing runtime call sites still accept one slug. The active schema resolves
    ``repo`` as an ordered list; repo mount/runtime code should consume the
    full list directly.
    """
    resolved = load_resolved_config(group_folder)
    slug = _first_repo(resolved)
    logger.debug(
        "Checked repo access",
        folder=group_folder,
        slug=slug,
    )
    return slug


def get_repo_access_groups(folders: Iterable[str]) -> dict[str, list[str]]:
    """Return a mapping of resolved repo slug → list of group folder names."""
    result: dict[str, list[str]] = {}
    for folder in folders:
        resolved = load_resolved_config(folder)
        for slug in resolved.repo if resolved else []:
            result.setdefault(slug, []).append(folder)
    return result


async def _pause_orphaned_tasks(desired_job_task_ids: set[str]) -> None:
    """Pause config-owned job tasks without a matching config declaration."""
    all_tasks = await get_all_tasks()
    for task in all_tasks:
        if task.status != "active":
            continue
        if task.id in desired_job_task_ids:
            continue
        if not task.id.startswith(("job-", "periodic-")):
            continue
        await update_task(task.id, {"status": "paused"})
        logger.info(
            "Paused orphaned config job task",
            task_id=task.id,
            folder=task.group_folder,
        )


async def reconcile_automation_jobs(
    workspaces: dict[str, WorkspaceProfile],
    settings: Settings,
) -> None:
    """Reconcile file-backed agent jobs without changing workspace topology."""
    desired_job_task_ids = await reconcile_agent_jobs(
        workspaces,
        settings,
        lambda folder: load_resolved_config(folder, settings=settings),
    )
    await _pause_orphaned_tasks(desired_job_task_ids)


async def reconcile_workspaces(  # noqa: PLR0913 - lifecycle cleanup joins existing registration callbacks.
    workspaces: dict[str, WorkspaceProfile],
    channels: list[Channel],
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
    unregister_fn: Callable[[str], Awaitable[None]] | None = None,
    rebind_fn: Callable[[WorkspaceProfile], Awaitable[None]] | None = None,
    retire_fn: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Ensure workspace state matches personalized desired state.

    Idempotent — safe to run on every startup. For each config-driven resource:
      1. Workspace registrations — create missing, remove orphaned
      2. Scheduled tasks — create missing, update changed, pause orphaned
    """
    s = get_settings()
    specs = _workspace_specs()
    folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in workspaces.items()}

    reconciled = 0
    for folder, spec in specs.items():
        config = _workspace_spec_config(spec)
        resolved = load_resolved_config(folder)
        if resolved is None:
            continue
        display_name = resolve_display_name(folder)

        jid = await ensure_workspace_registered(
            folder,
            config,
            resolved,
            display_name,
            workspaces,
            folder_to_jid,
            channels,
            s,
            register_fn,
            rebind_fn,
        )
        if jid is None:
            continue

        await sync_workspace_profile(jid, workspaces, folder, display_name, config, resolved)

        reconciled += 1

    if reconciled:
        logger.info("Workspaces reconciled", count=reconciled)

    thread_actions = await reconcile_workspace_threads(
        workspaces,
        {folder: _workspace_spec_config(spec) for folder, spec in specs.items()},
        channels,
        register_fn,
        rebind_fn=rebind_fn,
    )
    if thread_actions:
        logger.info("Workspace child threads reconciled", count=len(thread_actions))

    await reconcile_automation_jobs(workspaces, s)
    if unregister_fn is not None:
        await remove_orphaned_workspace_registrations(
            {*specs, *get_settings().workspace_names()},
            set(_runtime_policies),
            workspaces,
            channels,
            unregister_fn,
            retire_fn,
        )


# ---------------------------------------------------------------------------
# TOML writer
# ---------------------------------------------------------------------------


def add_workspace_to_toml(folder: str, config: WorkspaceConfig) -> None:
    """Programmatically write one versioned workspace declaration."""
    if Path(folder).name != folder or not folder or folder.startswith("."):
        raise ValueError(f"Invalid workspace name: {folder!r}")
    data = config.model_dump(exclude_none=True, exclude_defaults=True)
    candidate = get_settings().model_dump(mode="python")
    candidate["workspaces"][folder] = data
    _runtime.validate_settings_mapping(candidate)

    workspace_path = Path("data/personalization/workspaces") / f"{folder}.toml"
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc.add("schema_version", tomlkit.item(1))
    workspace_table = tomlkit.table()
    for key, value in data.items():
        workspace_table.add(key, value)
    doc.add("workspace", workspace_table)
    write_text_atomic(workspace_path, tomlkit.dumps(doc))
    reset_settings()


def update_profile_skill_policy(profile_name: str, skill_name: str, *, grant: bool) -> None:
    """Persist a named learned-skill decision in one workspace profile."""
    toml_path = get_settings().project_root / "data" / "personalization" / "pynchy.toml"

    def _mutate(doc: tomlkit.TOMLDocument) -> None:
        profiles = doc.get("profiles")
        if profiles is None or profile_name not in profiles:
            raise ValueError(f"Profile '{profile_name}' is not configured")
        profile = cast("Any", profiles[profile_name])
        skills = [str(value) for value in profile.get("skills", [])]
        denied_skills = [str(value) for value in profile.get("denied_skills", [])]
        if grant:
            skills = _deduplicate_preserving_order([*skills, skill_name])
            denied_skills = [name for name in denied_skills if name != skill_name]
        else:
            skills = [name for name in skills if name != skill_name]
            denied_skills = _deduplicate_preserving_order([*denied_skills, skill_name])
        profile["skills"] = skills
        profile["denied_skills"] = denied_skills

    mutate_config_toml(toml_path, _mutate)
    reset_settings()


def _deduplicate_preserving_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def add_job_to_toml(job_name: str, config: JobConfig) -> None:
    """Programmatically add one file-backed automation."""
    if Path(job_name).name != job_name or not job_name or job_name.startswith("."):
        raise ValueError(f"Invalid automation name: {job_name!r}")
    automation_path = Path("data/personalization/automations") / job_name / "config.toml"
    automation_path.parent.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc.add("schema_version", tomlkit.item(1))
    job_table = tomlkit.table()
    for key, value in config.model_dump(exclude_none=True, exclude_defaults=True).items():
        job_table.add(key, value)
    doc.add("job", job_table)
    write_text_atomic(automation_path, tomlkit.dumps(doc))

    reset_settings()
