"""Workspace configuration — reads from config.toml via Settings.

Workspaces are defined in [sandbox.<folder_name>] sections of config.toml.
Runtime creation (e.g. via IPC) writes new sections using add_workspace_to_toml().
"""

# FIXME: Rename "workspace" -> "sandbox" across config + codebase.

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pynchy.config import get_settings, reset_settings
from pynchy.config_models import WorkspaceConfig
from pynchy.config_refs import connection_ref_from_parts, parse_chat_ref
from pynchy.db import (
    create_task,
    get_active_task_for_group,
    get_all_tasks,
    set_workspace_profile,
    update_task,
)
from pynchy.logger import logger
from pynchy.utils import compute_next_run

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
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid workspace config from plugin", folder=folder, err=str(exc))
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


async def _resolve_configured_jid(
    *,
    config: WorkspaceConfig,
    channels: list[Channel],
    allow_create: bool,
) -> str | None:
    chat_ref = parse_chat_ref(config.chat)
    if chat_ref is None:
        logger.warning("Invalid chat ref in workspace config", chat=config.chat)
        return None

    connection_name = connection_ref_from_parts(chat_ref.platform, chat_ref.name)
    channel = next((ch for ch in channels if getattr(ch, "name", None) == connection_name), None)
    if channel is None:
        logger.warning(
            "Configured connection not found for workspace",
            connection=connection_name,
        )
        return None

    jid: str | None = None
    if hasattr(channel, "resolve_chat_jid"):
        try:
            jid = await channel.resolve_chat_jid(chat_ref.chat)
        except Exception as exc:
            logger.warning(
                "Failed to resolve chat JID",
                connection=connection_name,
                chat=chat_ref.chat,
                err=str(exc),
            )
            jid = None

    if jid is None and allow_create and hasattr(channel, "create_group"):
        try:
            jid = await channel.create_group(chat_ref.chat)
            logger.info(
                "Created chat group for workspace",
                connection=connection_name,
                chat=chat_ref.chat,
                jid=jid,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create chat group for workspace",
                connection=connection_name,
                chat=chat_ref.chat,
                err=str(exc),
            )
            jid = None

    if jid is None:
        logger.warning(
            "Chat not found for workspace",
            connection=connection_name,
            chat=chat_ref.chat,
        )
    return jid


def load_workspace_config(group_folder: str) -> WorkspaceConfig | None:
    """Read workspace config for a group from Settings.

    Returns None if the group has no [sandbox.<folder>] section in config.toml.
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


async def reconcile_workspaces(
    workspaces: dict[str, WorkspaceProfile],
    channels: list[Channel],
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
    register_alias_fn: Callable[[str, str, str], Awaitable[None]] | None = None,
    get_channel_jid_fn: Callable[[str, str], str | None] | None = None,
    unregister_fn: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Ensure workspace state matches config.toml — create, update, AND clean up.

    Idempotent — safe to run on every startup. For each config-driven resource:
      1. Workspace registrations — create missing, remove orphaned
      2. Scheduled tasks — create missing, update changed, pause orphaned
      3. Channel aliases — create missing (TODO: clean up orphaned)
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

        # 1. Ensure the group is registered (bind to configured chat)
        jid = folder_to_jid.get(folder)
        chat_ref = parse_chat_ref(config.chat)
        connection_name = (
            connection_ref_from_parts(chat_ref.platform, chat_ref.name) if chat_ref else ""
        )
        allow_create = bool(
            s.command_center.connection and connection_name == s.command_center.connection
        )

        expected_jid = await _resolve_configured_jid(
            config=config,
            channels=channels,
            allow_create=allow_create,
        )

        if jid is None:
            if expected_jid is None:
                logger.warning(
                    "Workspace chat unavailable, skipping registration",
                    folder=folder,
                )
                continue
            jid = expected_jid
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
                "Registered workspace for configured chat",
                name=display_name,
                folder=folder,
                is_admin=config.is_admin,
            )
        elif expected_jid and jid != expected_jid:
            logger.warning(
                "Workspace JID mismatch with configured chat",
                folder=folder,
                registered_jid=jid,
                expected_jid=expected_jid,
            )

        # 1b. Update existing workspace profile if config fields changed
        if jid is not None and jid in workspaces:
            profile = workspaces[jid]
            changed: dict[str, Any] = {}
            if profile.name != display_name:
                changed["name"] = display_name
            if profile.is_admin != config.is_admin:
                changed["is_admin"] = config.is_admin
            if changed:
                updated = replace(profile, **changed)
                workspaces[jid] = updated
                await set_workspace_profile(updated)
                logger.info(
                    "Updated workspace profile",
                    folder=folder,
                    changed=list(changed.keys()),
                )

        # 2. For periodic agents, ensure scheduled task exists and is up to date
        if not config.is_periodic:
            reconciled += 1
            continue

        existing_task = await get_active_task_for_group(folder)

        if existing_task is None:
            next_run = compute_next_run("cron", config.schedule, s.timezone)

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
                updates["next_run"] = compute_next_run("cron", config.schedule, s.timezone)
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

    # 3. Pause orphaned tasks — workspace removed from config or no longer periodic
    periodic_folders = {f for f, sp in specs.items() if sp.config.is_periodic}
    all_tasks = await get_all_tasks()
    for task in all_tasks:
        if task.status == "active" and task.group_folder not in periodic_folders:
            await update_task(task.id, {"status": "paused"})
            logger.info(
                "Paused orphaned scheduled task",
                task_id=task.id,
                folder=task.group_folder,
            )

    # 4. Remove orphaned workspace registrations — in DB but not in config.
    #    Admin workspaces are exempt: created dynamically at first boot without
    #    a config entry.
    if unregister_fn is not None:
        config_folders = set(specs.keys())
        for jid, profile in list(workspaces.items()):
            if profile.folder not in config_folders and not profile.is_admin:
                await unregister_fn(jid)
                logger.info(
                    "Removed orphaned workspace registration",
                    folder=profile.folder,
                    jid=jid,
                )


# ---------------------------------------------------------------------------
# TOML writer
# ---------------------------------------------------------------------------


def add_workspace_to_toml(folder: str, config: WorkspaceConfig) -> None:
    """Programmatically add a sandbox to config.toml using tomlkit.

    Preserves existing comments and formatting. Creates [sandbox.<folder>]
    section. Resets the settings cache so next get_settings() picks it up.
    """
    from pathlib import Path

    import tomlkit

    toml_path = Path("config.toml")
    doc = tomlkit.parse(toml_path.read_text()) if toml_path.exists() else tomlkit.document()

    if "sandbox" not in doc:
        doc.add("sandbox", tomlkit.table(is_super_table=True))

    ws_table = tomlkit.table()
    data = config.model_dump(exclude_none=True, exclude_defaults=True)
    for key, value in data.items():
        ws_table.add(key, value)

    doc["sandbox"][folder] = ws_table  # type: ignore[index]

    # Ensure the referenced chat exists under [connection.*] if possible.
    chat_ref = parse_chat_ref(config.chat)
    if chat_ref is not None:
        if "connection" not in doc:
            logger.warning("Config missing [connection] section; chat not added", chat=config.chat)
        else:
            connection_tbl = doc["connection"]
            if chat_ref.platform not in connection_tbl:
                logger.warning(
                    "Config missing connection platform; chat not added",
                    platform=chat_ref.platform,
                )
            else:
                platform_tbl = connection_tbl[chat_ref.platform]
                if chat_ref.name not in platform_tbl:
                    logger.warning(
                        "Config missing connection; chat not added",
                        connection=connection_ref_from_parts(chat_ref.platform, chat_ref.name),
                    )
                else:
                    conn_tbl = platform_tbl[chat_ref.name]
                    if "chat" not in conn_tbl:
                        conn_tbl.add("chat", tomlkit.table(is_super_table=True))
                    chat_tbl = conn_tbl["chat"]
                    if chat_ref.chat not in chat_tbl:
                        chat_tbl.add(chat_ref.chat, tomlkit.table())

    toml_path.write_text(tomlkit.dumps(doc))

    # Reset so next get_settings() re-reads the file
    reset_settings()
