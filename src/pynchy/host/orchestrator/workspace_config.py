"""Workspace configuration — reads from config.toml via Settings.

Workspaces are defined in [workspaces.<name>] sections of config.toml.
Runtime creation (e.g. via IPC) writes sections using add_workspace_to_toml().
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pynchy.config import get_settings, reset_settings
from pynchy.config.discord_refs import parse_discord_chat_target
from pynchy.config.merge import ResolvedSandboxConfig
from pynchy.config.models import WorkspaceConfig
from pynchy.config.refs import connection_ref_from_parts, parse_chat_ref
from pynchy.config.settings import Settings
from pynchy.config.toml_io import mutate_config_toml
from pynchy.host.orchestrator.config_jobs import reconcile_agent_jobs
from pynchy.host.orchestrator.workspace_registration import (
    ensure_workspace_registered,
    resolve_display_name,
    sync_workspace_profile,
)
from pynchy.logger import logger
from pynchy.state import (
    create_task,
    get_active_task_for_group,
    get_all_tasks,
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
        is_admin=config.is_admin or False,
        repo_access=config.repo_access,
        is_periodic=config.is_periodic,
    )
    return config


def load_resolved_config(group_folder: str) -> ResolvedSandboxConfig | None:
    """Load and merge the full config cascade for a workspace.

    Returns None if the group has no config.
    """
    from pynchy.config.merge import merge_sandbox_config

    ws = load_workspace_config(group_folder)
    if ws is None:
        return None

    s = get_settings()
    profile = None
    if ws.profile:
        profile = s.profiles.get(ws.profile)

    return merge_sandbox_config(s.universal, profile, ws)


def get_repo_access(group_folder: str) -> str | None:
    """Return the repo_access slug for a group folder, or None if not configured.

    Uses the three-tier merge cascade so that repo_access inherited from a
    profile is correctly resolved.
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
    profile is correctly resolved.
    """
    result: dict[str, list[str]] = {}
    for folder in folders:
        slug = get_repo_access(folder)
        if slug:
            result.setdefault(slug, []).append(folder)
    return result


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


async def _pause_orphaned_tasks(
    specs: dict[str, WorkspaceSpec], desired_job_task_ids: set[str]
) -> None:
    """Pause active scheduled tasks whose workspace is not periodic/configured."""
    periodic_folders = {f for f, sp in specs.items() if sp.config.is_periodic}
    all_tasks = await get_all_tasks()
    for task in all_tasks:
        if task.status != "active":
            continue
        if task.id in desired_job_task_ids:
            continue
        if task.group_folder not in periodic_folders:
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
        resolved = load_resolved_config(folder)
        if resolved is None:
            continue
        context_mode = resolved.context_mode
        resolved_repo_access = resolved.repo_access
        display_name = resolve_display_name(folder, config, resolved_repo_access)

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

        if not config.is_periodic:
            reconciled += 1
            continue

        await _reconcile_periodic_task(folder, config, jid, resolved_repo_access, context_mode, s)
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
    import tomlkit
    from tomlkit.items import Table

    toml_path = Path("config.toml")

    def _mutate(doc: Any) -> None:
        if "workspaces" not in doc:
            doc.add("workspaces", tomlkit.table(is_super_table=True))

        ws_table = tomlkit.table()
        data = config.model_dump(exclude_none=True, exclude_defaults=True)
        for key, value in data.items():
            ws_table.add(key, value)

        doc["workspaces"][folder] = ws_table

        # Ensure the referenced chat exists under [connection.*] if possible.
        chat_ref = parse_chat_ref(config.chat)
        if chat_ref is None:
            return
        if "connection" not in doc:
            logger.warning("Config missing [connection] section; chat not added", chat=config.chat)
            return
        connection_tbl = cast(Table, doc["connection"])
        if chat_ref.platform not in connection_tbl:
            logger.warning(
                "Config missing connection platform; chat not added",
                platform=chat_ref.platform,
            )
            return
        platform_tbl = cast(Table, connection_tbl[chat_ref.platform])
        if chat_ref.name not in platform_tbl:
            logger.warning(
                "Config missing connection; chat not added",
                connection=connection_ref_from_parts(chat_ref.platform, chat_ref.name),
            )
            return
        conn_tbl = cast(Table, platform_tbl[chat_ref.name])
        _ensure_chat_table(conn_tbl, chat_ref.platform, chat_ref.chat)

    mutate_config_toml(toml_path, _mutate)

    # Reset so next get_settings() re-reads the file
    reset_settings()


def _ensure_toml_table(parent: Any, key: str, *, super_table: bool = False) -> Any:
    """Return the TOML table at key, creating it when absent."""
    import tomlkit
    from tomlkit.items import Table

    if key not in parent:
        parent.add(key, tomlkit.table(is_super_table=super_table))
    value = parent[key]
    if not isinstance(value, Table):
        raise TypeError(f"Expected TOML table at {key!r}")
    return value


def _ensure_chat_table(conn_tbl: Any, platform: str, chat: str) -> None:
    chat_tbl = _ensure_toml_table(conn_tbl, "chat", super_table=True)
    if platform == "discord":
        _ensure_discord_chat_table(chat_tbl, chat)
        return
    if chat not in chat_tbl:
        import tomlkit

        chat_tbl.add(chat, tomlkit.table())


def _ensure_discord_chat_table(chat_tbl: Any, chat: str) -> None:
    import tomlkit

    target = parse_discord_chat_target(chat)
    if target is None or target.kind == "direct":
        return

    guild_tbl = _ensure_toml_table(chat_tbl, target.guild_id or "")
    if "require_mention" not in guild_tbl:
        require_mention = True
        guild_tbl.add("require_mention", require_mention)
    channels_tbl = _ensure_toml_table(guild_tbl, "channels", super_table=True)
    if target.target_id not in channels_tbl:
        channel_tbl = tomlkit.table()
        enabled = True
        channel_tbl.add("enabled", enabled)
        channels_tbl.add(target.target_id, channel_tbl)
