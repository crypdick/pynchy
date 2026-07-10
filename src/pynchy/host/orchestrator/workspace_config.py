"""Workspace configuration — reads from config.toml via Settings.

Workspaces are defined in [workspaces.<name>] sections of config.toml.
Runtime creation (e.g. via IPC) writes sections using add_workspace_to_toml().
"""

from __future__ import annotations

import re
from collections.abc import (
    Awaitable,  # noqa: TC003, RUF100 - beartype resolves workspace config annotations at runtime.
    Callable,  # noqa: TC003, RUF100 - beartype resolves workspace config annotations at runtime.
    Iterable,  # noqa: TC003, RUF100 - beartype resolves workspace config annotations at runtime.
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pluggy  # noqa: TC002, RUF100 - beartype resolves plugin-manager annotations at runtime.
import tomlkit

from pynchy.config import get_settings, reset_settings
from pynchy.config.jobs import (
    JobConfig,  # noqa: TC001, RUF100 - beartype resolves workspace config annotations at runtime.
)
from pynchy.config.merge import (
    ResolvedWorkspaceConfig,  # noqa: TC001, RUF100 - beartype resolves workspace config annotations at runtime.
)
from pynchy.config.models import WorkspaceConfig
from pynchy.config.toml_io import mutate_config_toml
from pynchy.host.orchestrator.config_jobs import reconcile_agent_jobs
from pynchy.host.orchestrator.workspace_registration import (
    ensure_workspace_registered,
    resolve_display_name,
    sync_workspace_profile,
)
from pynchy.logger import logger
from pynchy.state import (
    get_all_tasks,
    update_task,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves workspace config annotations at runtime.
    Channel,
    WorkspaceProfile,
)


@dataclass(frozen=True)
class WorkspaceSpec:
    """Resolved workspace definition."""

    config: WorkspaceConfig


@dataclass
class _WorkspaceConfigState:
    plugin_workspace_specs: dict[str, WorkspaceSpec] = field(default_factory=dict)


_state = _WorkspaceConfigState()
_DYNAMIC_THREAD_DELIMITER = "__thread_"


def _safe_folder_fragment(value: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return fragment or "thread"


def dynamic_thread_folder(parent_folder: str, thread_jid: str) -> str:
    """Return the isolated runtime folder for a dynamic thread workspace."""
    return f"{parent_folder}{_DYNAMIC_THREAD_DELIMITER}{_safe_folder_fragment(thread_jid)}"


def _parent_folder_for_dynamic_thread(folder: str) -> str | None:
    parent, sep, _child = folder.partition(_DYNAMIC_THREAD_DELIMITER)
    if not sep or not parent:
        return None
    return parent


def static_workspace_folder(folder: str) -> str:
    """Return the configured parent workspace folder for dynamic thread folders."""
    return _parent_folder_for_dynamic_thread(folder) or folder


def configure_plugin_workspaces(plugin_manager: pluggy.PluginManager | None) -> None:
    """Cache workspace specs exported by plugins.

    Plugin workspace configs are merged with config.toml in `load_workspace_config`.
    """
    _state.plugin_workspace_specs.clear()
    if plugin_manager is None:
        return

    for spec in plugin_manager.hook.pynchy_workspace_spec():
        if not isinstance(spec, dict):
            logger.warning("Ignoring invalid workspace plugin spec", spec_type=type(spec).__name__)
            continue

        folder = spec.get("folder")
        config_data = spec.get("config")
        if not isinstance(folder, str) or not isinstance(config_data, dict):
            logger.warning("Ignoring malformed workspace plugin spec", spec=spec)
            continue

        try:
            parsed = WorkspaceConfig.model_validate(config_data)
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid workspace config from plugin", folder=folder, err=str(exc))
            continue

        _state.plugin_workspace_specs[folder] = WorkspaceSpec(config=parsed)


def _workspace_specs() -> dict[str, WorkspaceSpec]:
    """Return merged workspace specs from plugins and config.toml.

    User config always wins for config fields. Plugin `claude_md` remains attached
    so startup can seed missing files even when config is overridden by the user.
    """
    s = get_settings()
    merged = dict(_state.plugin_workspace_specs)
    for folder, cfg in s.workspaces.items():
        merged[folder] = WorkspaceSpec(config=cfg)
    return merged


def load_workspace_config(group_folder: str) -> WorkspaceConfig | None:
    """Read workspace config for a group from Settings.

    Returns None if the group has no [workspaces.<name>] section in config.toml.
    """
    specs = _workspace_specs()
    spec = specs.get(group_folder)
    if spec is None:
        parent_folder = _parent_folder_for_dynamic_thread(group_folder)
        if parent_folder is not None:
            spec = specs.get(parent_folder)
    if spec is None:
        return None
    config = spec.config

    logger.debug(
        "Loaded workspace config",
        folder=group_folder,
        profiles=list(config.profiles),
    )
    return config


def load_resolved_config(group_folder: str) -> ResolvedWorkspaceConfig | None:
    """Load and merge the composable profiles for a workspace.

    Returns None if the group has no config.
    """
    if load_workspace_config(group_folder) is None:
        return None

    s = get_settings()
    resolved = s.resolved_workspace_config(group_folder)
    if resolved is not None:
        return resolved
    parent_folder = _parent_folder_for_dynamic_thread(group_folder)
    if parent_folder is not None:
        return s.resolved_workspace_config(parent_folder)
    return None


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


async def _pause_orphaned_tasks(
    specs: dict[str, WorkspaceSpec], desired_job_task_ids: set[str]
) -> None:
    """Pause config-owned job tasks without a matching config declaration."""
    del specs
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


async def _remove_orphaned_workspaces(
    specs: dict[str, WorkspaceSpec],
    workspaces: dict[str, WorkspaceProfile],
    unregister_fn: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Remove workspace registrations in the DB but not in config (admin exempt).

    Admin workspaces are created dynamically at first boot without a config entry.
    """
    if unregister_fn is None:
        return
    config_folders = set(specs.keys())
    for jid, profile in list(workspaces.items()):
        if profile.folder in config_folders or profile.is_admin:
            continue
        parent_folder = _parent_folder_for_dynamic_thread(profile.folder)
        if parent_folder in config_folders:
            continue
        await unregister_fn(jid)
        logger.info("Removed orphaned workspace registration", folder=profile.folder, jid=jid)


async def reconcile_workspaces(
    workspaces: dict[str, WorkspaceProfile],
    channels: list[Channel],
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
    unregister_fn: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Ensure workspace state matches config.toml — create, update, AND clean up.

    Idempotent — safe to run on every startup. For each config-driven resource:
      1. Workspace registrations — create missing, remove orphaned
      2. Scheduled tasks — create missing, update changed, pause orphaned
    """
    s = get_settings()
    specs = _workspace_specs()
    folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in workspaces.items()}

    reconciled = 0
    for folder, spec in specs.items():
        config = spec.config
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
        )
        if jid is None:
            continue

        await sync_workspace_profile(jid, workspaces, folder, display_name, config, resolved)

        reconciled += 1

    if reconciled:
        logger.info("Workspaces reconciled", count=reconciled)

    desired_job_task_ids = await reconcile_agent_jobs(workspaces, s, load_resolved_config)
    await _pause_orphaned_tasks(specs, desired_job_task_ids)
    await _remove_orphaned_workspaces(specs, workspaces, unregister_fn)


# ---------------------------------------------------------------------------
# TOML writer
# ---------------------------------------------------------------------------


def add_workspace_to_toml(folder: str, config: WorkspaceConfig) -> None:
    """Programmatically add a workspace to config.toml using tomlkit.

    Preserves existing comments and formatting. Creates [workspaces.<folder>]
    section. Resets the settings cache so next get_settings() picks it up.
    """
    toml_path = Path("config.toml")

    def _mutate(doc: tomlkit.TOMLDocument) -> None:
        if "workspaces" not in doc:
            doc.add("workspaces", tomlkit.table(is_super_table=True))

        ws_table = tomlkit.table()
        data = config.model_dump(exclude_none=True, exclude_defaults=True)
        for key, value in data.items():
            ws_table.add(key, value)

        cast("Any", doc["workspaces"])[folder] = ws_table

    mutate_config_toml(toml_path, _mutate)

    # Reset so next get_settings() re-reads the file
    reset_settings()


def add_job_to_toml(job_name: str, config: JobConfig) -> None:
    """Programmatically add a job to config.toml using tomlkit."""
    toml_path = Path("config.toml")

    def _mutate(doc: tomlkit.TOMLDocument) -> None:
        if "jobs" not in doc:
            doc.add("jobs", tomlkit.table(is_super_table=True))

        job_table = tomlkit.table()
        data = config.model_dump(exclude_none=True, exclude_defaults=True)
        for key, value in data.items():
            job_table.add(key, value)

        cast("Any", doc["jobs"])[job_name] = job_table

    mutate_config_toml(toml_path, _mutate)

    reset_settings()
