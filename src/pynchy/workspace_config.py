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

from pynchy.config import get_settings
from pynchy.config_models import WorkspaceConfig
from pynchy.db import create_task, get_active_task_for_group, update_task
from pynchy.logger import logger

if TYPE_CHECKING:
    import pluggy

    from pynchy.types import Channel, WorkspaceProfile


@dataclass(frozen=True)
class WorkspaceSpec:
    """Resolved workspace definition."""

    config: WorkspaceConfig


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

        _plugin_workspace_specs[folder] = WorkspaceSpec(config=parsed)


def _workspace_specs() -> dict[str, WorkspaceSpec]:
    """Return merged workspace specs from plugins and config.toml.

    User config always wins for config fields. Plugin `claude_md` remains attached
    so startup can seed missing files even when config is overridden by the user.
    """
    s = get_settings()
    merged = dict(_plugin_workspace_specs)
    for folder, cfg in s.workspaces.items():
        merged[folder] = WorkspaceSpec(config=cfg)
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
    if config.context_mode is None:
        config = config.model_copy(update={"context_mode": s.workspace_defaults.context_mode})

    logger.debug(
        "Loaded workspace config",
        folder=group_folder,
        is_admin=config.is_admin,
        repo_access=config.repo_access,
        is_periodic=config.is_periodic,
    )
    return config


def get_repo_access(group: WorkspaceProfile) -> str | None:
    """Return the repo_access slug for a group, or None if not configured.

    Unlike the old has_pynchy_repo_access, admin groups no longer get implicit
    access — they must set repo_access explicitly in config.toml.
    """
    config = load_workspace_config(group.folder)
    slug = config.repo_access if config else None
    logger.debug(
        "Checked repo access",
        folder=group.folder,
        slug=slug,
    )
    return slug


def get_repo_access_groups(workspaces: dict[str, Any]) -> dict[str, list[str]]:
    """Return a mapping of slug → list of group folder names with repo_access.

    Only groups with an explicit repo_access slug in config.toml are included.
    """
    result: dict[str, list[str]] = {}
    for profile in workspaces.values():
        config = load_workspace_config(profile.folder)
        if config and config.repo_access:
            result.setdefault(config.repo_access, []).append(profile.folder)
    return result


async def create_channel_aliases(
    jid: str,
    name: str,
    channels: list[Channel],
    register_alias_fn: Callable[[str, str, str], Awaitable[None]],
    get_channel_jid_fn: Callable[[str, str], str | None] | None = None,
) -> int:
    """Create aliases for a single JID across channels that support it.

    This is the single code path for all channel alias creation — used by
    workspace reconciliation, admin group setup, and batch alias backfill.

    For each channel that supports ``create_group``, skips if the channel
    already owns the JID or an alias already exists, then creates and registers
    a new alias.

    Returns the number of aliases created.
    """
    created = 0
    for ch in channels:
        if not hasattr(ch, "create_group"):
            continue
        if ch.owns_jid(jid):
            continue
        if get_channel_jid_fn and get_channel_jid_fn(jid, ch.name):
            continue
        try:
            alias_jid = await ch.create_group(name)
            await register_alias_fn(alias_jid, jid, ch.name)
            created += 1
            logger.info(
                "Created channel alias",
                channel=ch.name,
                alias_jid=alias_jid,
                canonical_jid=jid,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create channel alias",
                channel=ch.name,
                canonical_jid=jid,
                err=str(exc),
            )
    return created


async def _ensure_aliases_for_all_groups(
    workspaces: dict[str, WorkspaceProfile],
    channels: list[Channel],
    register_alias_fn: Callable[[str, str, str], Awaitable[None]],
    get_channel_jid_fn: Callable[[str, str], str | None] | None = None,
) -> None:
    """Create missing channel aliases for every registered group.

    Groups created via channel auto-registration (e.g. a new chat group)
    won't have aliases on other channels. This fills the gaps.
    """
    created = 0
    for jid, group in workspaces.items():
        created += await create_channel_aliases(
            jid, group.name, channels, register_alias_fn, get_channel_jid_fn
        )
    if created:
        logger.info("Created missing channel aliases", count=created)


async def reconcile_workspaces(
    workspaces: dict[str, WorkspaceProfile],
    channels: list[Channel],
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
    register_alias_fn: Callable[[str, str, str], Awaitable[None]] | None = None,
    get_channel_jid_fn: Callable[[str, str], str | None] | None = None,
) -> None:
    """Ensure tasks + chat groups exist for workspaces defined in config.toml.

    Idempotent — safe to run on every startup. Creates chat groups for
    any workspace with no DB entry, and manages scheduled tasks for periodic agents.
    Also creates JID aliases on channels that didn't create the primary JID.
    """
    from pynchy.types import WorkspaceProfile

    s = get_settings()
    specs = _workspace_specs()
    folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in workspaces.items()}

    reconciled = 0
    for folder, spec in specs.items():
        config = spec.config
        context_mode = config.context_mode or s.workspace_defaults.context_mode

        if config.name:
            display_name = config.name
        elif config.repo_access:
            # Slack channel names can't contain slashes — use double-dash convention
            display_name = config.repo_access.replace("/", "--")
        else:
            display_name = folder.replace("-", " ").title()

        # 1. Ensure the group is registered (create chat group if needed)
        jid = folder_to_jid.get(folder)
        if jid is None:
            default_name = (get_settings().channels.command_center or "").strip()
            channel = next(
                (
                    ch
                    for ch in channels
                    if getattr(ch, "name", None) == default_name and hasattr(ch, "create_group")
                ),
                None,
            ) or next(
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
            profile = WorkspaceProfile(
                jid=jid,
                name=display_name,
                folder=folder,
                trigger=f"@{s.agent.name}",
                added_at=datetime.now(UTC).isoformat(),
                is_admin=config.is_admin,
            )
            await register_fn(profile)
            folder_to_jid[folder] = jid
            logger.info(
                "Created chat group for workspace",
                name=display_name,
                folder=folder,
                is_admin=config.is_admin,
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
                    "repo_access": config.repo_access,
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
            if existing_task.repo_access != config.repo_access:
                updates["repo_access"] = config.repo_access
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
