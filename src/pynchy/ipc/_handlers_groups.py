"""IPC handlers for group registration, refresh, and periodic agent creation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from croniter import croniter

from pynchy.config import get_settings
from pynchy.db import create_task
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import register
from pynchy.logger import logger
from pynchy.types import ContainerConfig, WorkspaceProfile
from pynchy.utils import compute_next_run


async def _handle_register_group(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    if not is_admin:
        logger.warning(
            "Unauthorized register_group attempt blocked",
            source_group=source_group,
        )
        return

    jid = data.get("jid")
    name = data.get("name")
    folder = data.get("folder")
    trigger = data.get("trigger")

    if jid and name and folder and trigger:
        deps.register_workspace(
            WorkspaceProfile(
                jid=jid,
                name=name,
                folder=folder,
                trigger=trigger,
                added_at=datetime.now(UTC).isoformat(),
                container_config=ContainerConfig.from_dict(data["containerConfig"])
                if data.get("containerConfig")
                else None,
            ),
        )
    else:
        logger.warning(
            "Invalid register_group request - missing required fields",
            data=str(data),
        )


async def _handle_create_periodic_agent(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    """Create a periodic agent: folder, config.toml workspace, CLAUDE.md, chat group, and task."""
    if not is_admin:
        logger.warning(
            "Unauthorized create_periodic_agent attempt blocked",
            source_group=source_group,
        )
        return

    from pynchy.config import add_workspace_to_toml
    from pynchy.config_models import WorkspaceConfig

    s = get_settings()

    name = data.get("name")
    schedule = data.get("schedule")
    prompt = data.get("prompt")
    if not name or not schedule or not prompt:
        logger.warning("create_periodic_agent missing required fields", data=str(data))
        return

    if not croniter.is_valid(schedule):
        logger.warning("create_periodic_agent invalid cron", schedule=schedule)
        return

    context_mode = data.get("context_mode", "group")
    if context_mode not in ("group", "isolated"):
        context_mode = "group"

    claude_md = data.get("claude_md", f"You are the {name} periodic agent.")

    group_dir = s.groups_dir / name
    group_dir.mkdir(parents=True, exist_ok=True)

    config = WorkspaceConfig(
        name=name,
        schedule=schedule,
        prompt=prompt,
        context_mode=context_mode,
        trigger="always",
    )
    add_workspace_to_toml(name, config)

    claude_md_path = group_dir / "CLAUDE.md"
    if not claude_md_path.exists():
        claude_md_path.write_text(claude_md)

    channels = deps.channels()
    channel = next((ch for ch in channels if hasattr(ch, "create_group")), None)
    if channel is None:
        logger.warning("No channel supports create_group, periodic agent created without chat")
        return

    agent_display_name = name.replace("-", " ").title()
    jid = await channel.create_group(agent_display_name)

    profile = WorkspaceProfile(
        jid=jid,
        name=agent_display_name,
        folder=name,
        trigger=f"@{s.agent.name}",
        added_at=datetime.now(UTC).isoformat(),
    )
    deps.register_workspace(profile)

    next_run = compute_next_run("cron", schedule, s.timezone)
    task_id = f"periodic-{name}-{uuid.uuid4().hex[:8]}"

    await create_task(
        {
            "id": task_id,
            "group_folder": name,
            "chat_jid": jid,
            "prompt": prompt,
            "schedule_type": "cron",
            "schedule_value": schedule,
            "context_mode": context_mode,
            "next_run": next_run,
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
        }
    )

    logger.info(
        "Periodic agent created via IPC",
        name=name,
        schedule=schedule,
        task_id=task_id,
        jid=jid,
    )


register("register_group", _handle_register_group)
register("create_periodic_agent", _handle_create_periodic_agent)
