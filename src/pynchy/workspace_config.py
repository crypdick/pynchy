"""Workspace configuration — unified YAML schema, loader, and reconciliation.

Each group can have a groups/{name}/workspace.yaml that defines both workspace
identity (is_god, requires_trigger) and optional periodic scheduling. This
replaces the old periodic.yaml which only handled scheduling.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

import yaml
from croniter import croniter

from pynchy.config import ASSISTANT_NAME, GROUPS_DIR, TIMEZONE
from pynchy.db import create_task, get_active_task_for_group, update_task
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.types import Channel, RegisteredGroup


@dataclass
class WorkspaceConfig:
    is_god: bool = False
    requires_trigger: bool = True
    project_access: bool = False
    name: str | None = None  # defaults to folder name titlecased
    # Periodic scheduling (optional)
    schedule: str | None = None  # cron expression
    prompt: str | None = None
    context_mode: Literal["group", "isolated"] = "group"

    @property
    def is_periodic(self) -> bool:
        return self.schedule is not None and self.prompt is not None


def load_workspace_config(group_folder: str) -> WorkspaceConfig | None:
    """Read groups/{folder}/workspace.yaml, return None if not present."""
    path = GROUPS_DIR / group_folder / "workspace.yaml"
    if not path.exists():
        return None

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        # Empty file is valid — all defaults
        return WorkspaceConfig() if raw is None else None

    # Workspace identity fields
    is_god = bool(raw.get("is_god", False))
    requires_trigger = bool(raw.get("requires_trigger", True))
    project_access = bool(raw.get("project_access", False))
    name = raw.get("name")
    if name is not None:
        name = str(name)

    # Periodic scheduling fields (optional)
    schedule = raw.get("schedule")
    prompt = raw.get("prompt")

    if schedule is not None:
        schedule = str(schedule)
        if not croniter.is_valid(schedule):
            schedule = None

    if prompt is not None:
        prompt = str(prompt)

    context_mode = raw.get("context_mode", "group")
    if context_mode not in ("group", "isolated"):
        context_mode = "group"

    return WorkspaceConfig(
        is_god=is_god,
        requires_trigger=requires_trigger,
        project_access=project_access,
        name=name,
        schedule=schedule,
        prompt=prompt,
        context_mode=context_mode,
    )


def write_workspace_config(group_folder: str, config: WorkspaceConfig) -> Path:
    """Write a workspace.yaml file for a group. Returns the path written."""
    path = GROUPS_DIR / group_folder / "workspace.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}

    # Workspace identity fields — only write non-defaults
    if config.is_god:
        data["is_god"] = True
    if not config.requires_trigger:
        data["requires_trigger"] = False
    if config.project_access:
        data["project_access"] = True
    if config.name is not None:
        data["name"] = config.name

    # Periodic scheduling fields
    if config.schedule is not None:
        data["schedule"] = config.schedule
    if config.prompt is not None:
        data["prompt"] = config.prompt
    if config.context_mode != "group":
        data["context_mode"] = config.context_mode

    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False) if data else "")
    return path


def has_project_access(group: RegisteredGroup) -> bool:
    """Check if a group has project_access (god groups always do)."""
    if group.is_god:
        return True
    config = load_workspace_config(group.folder)
    return bool(config and config.project_access)


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
    """Scan groups/ for workspace.yaml files and ensure tasks + chat groups exist.

    Idempotent — safe to run on every startup. Creates WhatsApp groups for
    any folder with a workspace.yaml but no DB entry, and manages scheduled
    tasks for periodic agents.
    """
    from pynchy.types import RegisteredGroup as RG

    folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in registered_groups.items()}

    if not GROUPS_DIR.exists():
        return

    reconciled = 0
    for folder in sorted(GROUPS_DIR.iterdir()):
        if not folder.is_dir():
            continue

        config = load_workspace_config(folder.name)
        if config is None:
            continue

        display_name = config.name or folder.name.replace("-", " ").title()

        # 1. Ensure the group is registered (create chat group if needed)
        jid = folder_to_jid.get(folder.name)
        if jid is None:
            channel = next(
                (ch for ch in channels if hasattr(ch, "create_group")),
                None,
            )
            if channel is None:
                logger.warning(
                    "No channel supports create_group, skipping workspace",
                    folder=folder.name,
                )
                continue

            jid = await channel.create_group(display_name)
            group = RG(
                name=display_name,
                folder=folder.name,
                trigger=f"@{ASSISTANT_NAME}",
                added_at=datetime.now(UTC).isoformat(),
                requires_trigger=config.requires_trigger,
                is_god=config.is_god,
            )
            await register_fn(jid, group)
            folder_to_jid[folder.name] = jid
            logger.info(
                "Created chat group for workspace",
                name=display_name,
                folder=folder.name,
                is_god=config.is_god,
            )

        # 2. For periodic agents, ensure scheduled task exists and is up to date
        if not config.is_periodic:
            reconciled += 1
            continue

        existing_task = await get_active_task_for_group(folder.name)

        if existing_task is None:
            tz = ZoneInfo(TIMEZONE)
            cron = croniter(config.schedule, datetime.now(tz))
            next_run = cron.get_next(datetime).isoformat()

            task_id = f"periodic-{folder.name}-{uuid.uuid4().hex[:8]}"
            await create_task(
                {
                    "id": task_id,
                    "group_folder": folder.name,
                    "chat_jid": jid,
                    "prompt": config.prompt,
                    "schedule_type": "cron",
                    "schedule_value": config.schedule,
                    "context_mode": config.context_mode,
                    "project_access": config.project_access,
                    "next_run": next_run,
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            logger.info(
                "Created scheduled task for periodic agent",
                task_id=task_id,
                folder=folder.name,
                schedule=config.schedule,
            )
        else:
            updates: dict[str, Any] = {}
            if existing_task.schedule_value != config.schedule:
                updates["schedule_value"] = config.schedule
                tz = ZoneInfo(TIMEZONE)
                cron = croniter(config.schedule, datetime.now(tz))
                updates["next_run"] = cron.get_next(datetime).isoformat()
            if existing_task.prompt != config.prompt:
                updates["prompt"] = config.prompt
            if existing_task.project_access != config.project_access:
                updates["project_access"] = config.project_access
            if updates:
                await update_task(existing_task.id, updates)
                logger.info(
                    "Updated periodic agent task",
                    task_id=existing_task.id,
                    folder=folder.name,
                    changed=list(updates.keys()),
                )

        reconciled += 1

    if reconciled:
        logger.info("Workspaces reconciled", count=reconciled)
