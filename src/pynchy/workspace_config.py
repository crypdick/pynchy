"""Workspace configuration — reads from config.toml via Settings.

Workspaces are defined in [workspaces.<folder_name>] sections of config.toml.
Runtime creation (e.g. via IPC) writes new sections using add_workspace_to_toml().
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.config import WorkspaceConfig, add_workspace_to_toml, get_settings
from pynchy.db import create_task, get_active_task_for_group, update_task
from pynchy.logger import logger

if TYPE_CHECKING:
    import pluggy

    from pynchy.types import Channel, RegisteredGroup


@dataclass(frozen=True)
class WorkspaceSpec:
    """Resolved workspace definition with optional seeded CLAUDE.md."""

    config: WorkspaceConfig
    claude_md: str | None = None


_plugin_workspace_specs: dict[str, WorkspaceSpec] = {}


def configure_plugin_workspaces(plugin_manager: pluggy.PluginManager | None) -> None:
    """Cache workspace specs exported by plugins.

    Plugin workspace configs are merged with config.toml in `load_workspace_config`.
    """
    global _plugin_workspace_specs
    _plugin_workspace_specs = {}
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
        except Exception:
            logger.exception("Invalid workspace config from plugin", folder=folder)
            continue

        claude_md = spec.get("claude_md")
        if claude_md is not None and not isinstance(claude_md, str):
            logger.warning("Ignoring non-string claude_md in workspace spec", folder=folder)
            claude_md = None

        _plugin_workspace_specs[folder] = WorkspaceSpec(config=parsed, claude_md=claude_md)


def _workspace_specs() -> dict[str, WorkspaceSpec]:
    """Return merged workspace specs from plugins and config.toml.

    User config always wins for config fields. Plugin `claude_md` remains attached
    so startup can seed missing files even when config is overridden by the user.
    """
    s = get_settings()
    merged = dict(_plugin_workspace_specs)
    for folder, cfg in s.workspaces.items():
        plugin_spec = merged.get(folder)
        merged[folder] = WorkspaceSpec(
            config=cfg, claude_md=plugin_spec.claude_md if plugin_spec else None
        )
    return merged


def load_workspace_config(group_folder: str) -> WorkspaceConfig | None:
    """Read workspace config for a group from Settings.

    Returns None if the group has no [workspaces.<folder>] section in config.toml.
    """
    specs = _workspace_specs()
    spec = specs.get(group_folder)
    if spec is None:
        return None
    s = get_settings()
    config = spec.config

    # Apply workspace defaults for None fields
    if config.requires_trigger is None:
        config = config.model_copy(
            update={"requires_trigger": s.workspace_defaults.requires_trigger}
        )
    if config.context_mode is None:
        config = config.model_copy(update={"context_mode": s.workspace_defaults.context_mode})

    logger.debug(
        "Loaded workspace config",
        folder=group_folder,
        is_god=config.is_god,
        project_access=config.project_access,
        is_periodic=config.is_periodic,
    )
    return config


def write_workspace_config(group_folder: str, config: WorkspaceConfig) -> None:
    """Write a workspace config to config.toml."""
    add_workspace_to_toml(group_folder, config)
    logger.debug("Wrote workspace config to config.toml", folder=group_folder)


def has_project_access(group: RegisteredGroup) -> bool:
    """Check if a group has project_access (god groups always do)."""
    if group.is_god:
        return True
    config = load_workspace_config(group.folder)
    has_access = bool(config and config.project_access)
    logger.debug(
        "Checked project access",
        folder=group.folder,
        has_access=has_access,
    )
    return has_access


def get_project_access_folders(workspaces: dict[str, Any]) -> list[str]:
    """Return folder names for all workspaces with project_access."""
    folders: list[str] = []
    for profile in workspaces.values():
        if profile.is_god:
            folders.append(profile.folder)
            continue
        config = load_workspace_config(profile.folder)
        if config and config.project_access:
            folders.append(profile.folder)
    return folders


async def reconcile_workspaces(
    registered_groups: dict[str, RegisteredGroup],
    channels: list[Channel],
    register_fn: Callable[[str, RegisteredGroup], Awaitable[None]],
) -> None:
    """Ensure tasks + chat groups exist for workspaces defined in config.toml.

    Idempotent — safe to run on every startup. Creates WhatsApp groups for
    any workspace with no DB entry, and manages scheduled tasks for periodic agents.
    """
    from pynchy.types import RegisteredGroup as RG

    s = get_settings()
    specs = _workspace_specs()
    folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in registered_groups.items()}

    reconciled = 0
    for folder, spec in specs.items():
        config = spec.config
        # Apply defaults
        requires_trigger = (
            config.requires_trigger
            if config.requires_trigger is not None
            else s.workspace_defaults.requires_trigger
        )
        context_mode = config.context_mode or s.workspace_defaults.context_mode

        display_name = config.name or folder.replace("-", " ").title()

        if spec.claude_md:
            claude_path = s.groups_dir / folder / "CLAUDE.md"
            if not claude_path.exists():
                claude_path.parent.mkdir(parents=True, exist_ok=True)
                claude_path.write_text(spec.claude_md)
                logger.info("Seeded workspace CLAUDE.md from plugin", folder=folder)

        # 1. Ensure the group is registered (create chat group if needed)
        jid = folder_to_jid.get(folder)
        if jid is None:
            channel = next(
                (ch for ch in channels if hasattr(ch, "create_group")),
                None,
            )
            if channel is None:
                logger.warning(
                    "No channel supports create_group, skipping workspace",
                    folder=folder,
                )
                continue

            jid = await channel.create_group(display_name)
            group = RG(
                name=display_name,
                folder=folder,
                trigger=f"@{s.agent.name}",
                added_at=datetime.now(UTC).isoformat(),
                requires_trigger=requires_trigger,
                is_god=config.is_god,
            )
            await register_fn(jid, group)
            folder_to_jid[folder] = jid
            logger.info(
                "Created chat group for workspace",
                name=display_name,
                folder=folder,
                is_god=config.is_god,
            )

        # 2. For periodic agents, ensure scheduled task exists and is up to date
        if not config.is_periodic:
            reconciled += 1
            continue

        existing_task = await get_active_task_for_group(folder)

        if existing_task is None:
            tz = ZoneInfo(s.timezone)
            cron = croniter(config.schedule, datetime.now(tz))
            next_run = cron.get_next(datetime).astimezone(UTC).isoformat()

            task_id = f"periodic-{folder}-{uuid.uuid4().hex[:8]}"
            await create_task(
                {
                    "id": task_id,
                    "group_folder": folder,
                    "chat_jid": jid,
                    "prompt": config.prompt,
                    "schedule_type": "cron",
                    "schedule_value": config.schedule,
                    "context_mode": context_mode,
                    "project_access": config.project_access,
                    "next_run": next_run,
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            logger.info(
                "Created scheduled task for periodic agent",
                task_id=task_id,
                folder=folder,
                schedule=config.schedule,
            )
        else:
            updates: dict[str, Any] = {}
            if existing_task.schedule_value != config.schedule:
                updates["schedule_value"] = config.schedule
                tz = ZoneInfo(s.timezone)
                cron = croniter(config.schedule, datetime.now(tz))
                updates["next_run"] = cron.get_next(datetime).astimezone(UTC).isoformat()
            if existing_task.prompt != config.prompt:
                updates["prompt"] = config.prompt
            if existing_task.project_access != config.project_access:
                updates["project_access"] = config.project_access
            if updates:
                await update_task(existing_task.id, updates)
                logger.info(
                    "Updated periodic agent task",
                    task_id=existing_task.id,
                    folder=folder,
                    changed=list(updates.keys()),
                )

        reconciled += 1

    if reconciled:
        logger.info("Workspaces reconciled", count=reconciled)
