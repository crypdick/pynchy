"""Workspace configuration — reads from config.toml via Settings.

Workspaces are defined in [sandbox.<folder_name>] sections of config.toml.
Runtime creation (e.g. via IPC) writes sections using add_workspace_to_toml().
"""

from __future__ import annotations

# FIXME: Rename "workspace" -> "sandbox" across config + codebase.
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from pynchy.config import get_settings, reset_settings
from pynchy.config.merge import ResolvedSandboxConfig
from pynchy.config.models import WorkspaceConfig
from pynchy.config.refs import connection_ref_from_parts, parse_chat_ref
from pynchy.config.settings import Settings
from pynchy.logger import logger
from pynchy.state import (
    create_task,
    get_active_task_for_group,
    get_all_tasks,
    set_workspace_profile,
    update_task,
)
from pynchy.types import Channel, ScheduledTask, WorkspaceProfile
from pynchy.utils import compute_next_run

if TYPE_CHECKING:
    import pluggy


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

    jid = await _resolved_chat_jid(channel, connection_name, chat_ref.chat)
    channel_allows_create = bool(
        allow_create or getattr(channel, "auto_provision_configured_chats", False)
    )
    if jid is None and channel_allows_create:
        jid = await _created_chat_jid(channel, connection_name, chat_ref.chat)

    if jid is None:
        logger.warning(
            "Chat not found for workspace",
            connection=connection_name,
            chat=chat_ref.chat,
        )
    return jid


def _valid_jid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


async def _resolved_chat_jid(channel: Channel, connection_name: str, chat_name: str) -> str | None:
    if not hasattr(channel, "resolve_chat_jid"):
        return None

    try:
        return _valid_jid(await channel.resolve_chat_jid(chat_name))
    except Exception as exc:
        logger.warning(
            "Failed to resolve chat JID",
            connection=connection_name,
            chat=chat_name,
            err=str(exc),
        )
        return None


async def _created_chat_jid(channel: Channel, connection_name: str, chat_name: str) -> str | None:
    if not hasattr(channel, "create_group"):
        return None

    try:
        jid = _valid_jid(await channel.create_group(chat_name))
    except Exception as exc:
        logger.warning(
            "Failed to create chat group for workspace",
            connection=connection_name,
            chat=chat_name,
            err=str(exc),
        )
        return None

    if jid is not None:
        logger.info(
            "Created chat group for workspace",
            connection=connection_name,
            chat=chat_name,
            jid=jid,
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

    # Apply sandbox_universal defaults for None fields
    if config.context_mode is None:
        default_context_mode = s.sandbox_universal.context_mode or "group"
        config = config.model_copy(update={"context_mode": default_context_mode})

    logger.debug(
        "Loaded workspace config",
        folder=group_folder,
        is_admin=config.is_admin or False,
        repo_access=config.repo_access,
        is_periodic=config.is_periodic,
    )
    return config


def load_resolved_config(group_folder: str) -> ResolvedSandboxConfig | None:
    """Load and merge the full config cascade for a sandbox.

    Returns None if the group has no config.
    """
    from pynchy.config.merge import merge_sandbox_config

    ws = load_workspace_config(group_folder)
    if ws is None:
        return None

    s = get_settings()
    profile = None
    if ws.profile:
        profile = s.sandbox_profiles.get(ws.profile)

    return merge_sandbox_config(s.sandbox_universal, profile, ws)


def get_repo_access(group_folder: str) -> str | None:
    """Return the repo_access slug for a group folder, or None if not configured.

    Uses the three-tier merge cascade so that repo_access inherited from a
    sandbox profile is correctly resolved.
    """
    resolved = load_resolved_config(group_folder)
    slug = resolved.repo_access if resolved else None
    logger.debug(
        "Checked repo access",
        folder=group_folder,
        slug=slug,
    )
    return slug


def get_repo_access_groups(folders: Iterable[str]) -> dict[str, list[str]]:
    """Return a mapping of slug → list of group folder names with repo_access.

    Uses the three-tier merge cascade so that repo_access inherited from a
    sandbox profile is correctly resolved.
    """
    result: dict[str, list[str]] = {}
    for folder in folders:
        slug = get_repo_access(folder)
        if slug:
            result.setdefault(slug, []).append(folder)
    return result


def _resolve_display_name(
    folder: str, config: WorkspaceConfig, resolved_repo_access: str | None
) -> str:
    if config.name:
        return config.name
    if resolved_repo_access:
        # Slack channel names can't contain slashes — use double-dash convention
        return resolved_repo_access.replace("/", "--")
    return folder.replace("-", " ").title()


async def _ensure_workspace_registered(
    folder: str,
    config: WorkspaceConfig,
    display_name: str,
    workspaces: dict[str, WorkspaceProfile],
    folder_to_jid: dict[str, str],
    channels: list[Channel],
    s: Settings,
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
) -> str | None:
    """Ensure folder is registered to its configured chat. Returns the resolved jid, or None if unavailable."""
    from pynchy.types import WorkspaceProfile

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
            logger.warning("Workspace chat unavailable, skipping registration", folder=folder)
            return None
        jid = expected_jid
        profile = WorkspaceProfile(
            jid=jid,
            name=display_name,
            folder=folder,
            trigger=f"@{s.agent.name}",
            added_at=datetime.now(UTC).isoformat(),
            is_admin=config.is_admin or False,
        )
        await register_fn(profile)
        folder_to_jid[folder] = jid
        logger.info(
            "Registered workspace for configured chat",
            name=display_name,
            folder=folder,
            is_admin=config.is_admin or False,
        )
    elif expected_jid and jid != expected_jid:
        logger.warning(
            "Workspace JID mismatch with configured chat",
            folder=folder,
            registered_jid=jid,
            expected_jid=expected_jid,
        )

    return jid


async def _sync_workspace_profile(
    jid: str | None,
    workspaces: dict[str, WorkspaceProfile],
    folder: str,
    display_name: str,
    config: WorkspaceConfig,
) -> None:
    """Update the stored workspace profile if display name or admin flag changed."""
    if jid is None or jid not in workspaces:
        return
    profile = workspaces[jid]
    changed: dict[str, Any] = {}
    if profile.name != display_name:
        changed["name"] = display_name
    if profile.is_admin != (config.is_admin or False):
        changed["is_admin"] = config.is_admin or False
    if not changed:
        return
    updated = replace(profile, **changed)
    workspaces[jid] = updated
    await set_workspace_profile(updated)
    logger.info("Updated workspace profile", folder=folder, changed=list(changed.keys()))


async def _reconcile_periodic_task(
    folder: str,
    config: WorkspaceConfig,
    jid: str | None,
    resolved_repo_access: str | None,
    context_mode: str,
    s: Settings,
) -> None:
    """Create or update the scheduled task backing a periodic-agent workspace."""
    assert config.schedule is not None, "caller must guard with config.is_periodic"
    assert config.prompt is not None, "caller must guard with config.is_periodic"
    assert jid is not None, "caller guards jid is not None before _reconcile_periodic_task"
    existing_task = await get_active_task_for_group(folder)

    if existing_task is None:
        next_run = compute_next_run("cron", config.schedule, s.timezone)
        task_id = f"periodic-{folder}-{uuid.uuid4().hex[:8]}"
        await create_task(
            ScheduledTask(
                id=task_id,
                group_folder=folder,
                chat_jid=jid,
                prompt=config.prompt,
                schedule_type="cron",
                schedule_value=config.schedule,
                context_mode=cast('Literal["group", "isolated"]', context_mode),
                repo_access=resolved_repo_access,
                next_run=next_run,
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        logger.info(
            "Created scheduled task for periodic agent",
            task_id=task_id,
            folder=folder,
            schedule=config.schedule,
        )
        return

    updates: dict[str, Any] = {}
    if existing_task.schedule_value != config.schedule:
        updates["schedule_value"] = config.schedule
        updates["next_run"] = compute_next_run("cron", config.schedule, s.timezone)
    if existing_task.prompt != config.prompt:
        updates["prompt"] = config.prompt
    if existing_task.repo_access != resolved_repo_access:
        updates["repo_access"] = resolved_repo_access
    if not updates:
        return
    await update_task(existing_task.id, updates)
    logger.info(
        "Updated periodic agent task",
        task_id=existing_task.id,
        folder=folder,
        changed=list(updates.keys()),
    )


async def _pause_orphaned_tasks(specs: dict[str, WorkspaceSpec]) -> None:
    """Pause active scheduled tasks whose workspace is not periodic/configured."""
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
        if profile.folder not in config_folders and not profile.is_admin:
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
        context_mode = config.context_mode or s.sandbox_universal.context_mode or "group"
        resolved_repo_access = get_repo_access(folder)
        display_name = _resolve_display_name(folder, config, resolved_repo_access)

        jid = await _ensure_workspace_registered(
            folder, config, display_name, workspaces, folder_to_jid, channels, s, register_fn
        )
        if jid is None:
            continue

        await _sync_workspace_profile(jid, workspaces, folder, display_name, config)

        if not config.is_periodic:
            reconciled += 1
            continue

        await _reconcile_periodic_task(folder, config, jid, resolved_repo_access, context_mode, s)
        reconciled += 1

    if reconciled:
        logger.info("Workspaces reconciled", count=reconciled)

    await _pause_orphaned_tasks(specs)
    await _remove_orphaned_workspaces(specs, workspaces, unregister_fn)


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
    from tomlkit.items import Table

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
            connection_tbl = cast(Table, doc["connection"])
            if chat_ref.platform not in connection_tbl:
                logger.warning(
                    "Config missing connection platform; chat not added",
                    platform=chat_ref.platform,
                )
            else:
                platform_tbl = cast(Table, connection_tbl[chat_ref.platform])
                if chat_ref.name not in platform_tbl:
                    logger.warning(
                        "Config missing connection; chat not added",
                        connection=connection_ref_from_parts(chat_ref.platform, chat_ref.name),
                    )
                else:
                    conn_tbl = cast(Table, platform_tbl[chat_ref.name])
                    if "chat" not in conn_tbl:
                        conn_tbl.add("chat", tomlkit.table(is_super_table=True))
                    chat_tbl = cast(Table, conn_tbl["chat"])
                    if chat_ref.chat not in chat_tbl:
                        chat_tbl.add(chat_ref.chat, tomlkit.table())

    toml_path.write_text(tomlkit.dumps(doc))

    # Reset so next get_settings() re-reads the file
    reset_settings()
