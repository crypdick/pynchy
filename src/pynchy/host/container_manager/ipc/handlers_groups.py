"""IPC handlers for group registration, refresh, and periodic agent creation."""

from __future__ import annotations

import uuid
from collections.abc import (
    Sequence,  # noqa: TC003, RUF100 - beartype resolves group handler signatures at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves group setup paths at runtime.
from typing import Any, Protocol, cast, runtime_checkable

from croniter import croniter

from pynchy.config import get_settings
from pynchy.config.jobs import JobConfig
from pynchy.config.models import ChatRefStr, WorkspaceConfig
from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves group setup models at runtime.
)
from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # noqa: TC001, RUF100 - beartype resolves group handler signatures at runtime.
)
from pynchy.host.container_manager.ipc.protocol import (
    CreatePeriodicAgentRequest,
    RegisterGroupRequest,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.host.orchestrator import workspace_config
from pynchy.logger import logger
from pynchy.state import create_task
from pynchy.types import ScheduledTask, WorkspaceProfile


@runtime_checkable
class _CreateGroupChannel(Protocol):
    name: str

    async def create_group(self, name: str) -> str | None: ...


@dataclass(frozen=True)
class _PeriodicAgentSetup:
    settings: Settings
    command_center: str
    group_dir: Path
    chat_name: str
    chat_ref: ChatRefStr


async def _handle_register_group(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    if not is_admin:
        logger.warning(
            "Unauthorized register_group attempt blocked",
            source_group=source_group,
        )
        return

    request = RegisterGroupRequest.from_dict(data)
    if request is None:
        logger.warning(
            "Invalid register_group request - missing required fields",
            data=str(data),
        )
        return

    if not data.get("_cop_approved"):
        summary = f"name={request.name}, folder={request.folder}, trigger={request.trigger}"
        allowed = await cop_gate_module.cop_gate(
            "register_group",
            summary,
            data,
            source_group,
            deps,
        )
        if not allowed:
            return

    deps.register_workspace(
        WorkspaceProfile(
            jid=request.jid,
            name=request.name,
            folder=request.folder,
            trigger=request.trigger,
            added_at=datetime.now(UTC).isoformat(),
            container_config=request.container_config,
        ),
    )


async def _handle_create_periodic_agent(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    """Create a periodic agent: folder, config.toml workspace, CLAUDE.md, chat group, and task."""
    request = _periodic_agent_request(data, source_group=source_group, is_admin=is_admin)
    if request is None:
        return

    if not await _periodic_agent_cop_allowed(request, data, source_group, deps):
        return

    await _create_periodic_agent(request, deps)


def _periodic_agent_request(
    data: dict[str, Any], *, source_group: str, is_admin: bool
) -> CreatePeriodicAgentRequest | None:
    if not is_admin:
        logger.warning(
            "Unauthorized create_periodic_agent attempt blocked",
            source_group=source_group,
        )
        return None

    request = CreatePeriodicAgentRequest.from_dict(data)
    if request is None:
        logger.warning("create_periodic_agent missing required fields", data=str(data))
        return None

    if not croniter.is_valid(request.schedule):
        logger.warning("create_periodic_agent invalid cron", schedule=request.schedule)
        return None
    return request


async def _periodic_agent_cop_allowed(
    request: CreatePeriodicAgentRequest,
    data: dict[str, Any],
    source_group: str,
    deps: IpcDeps,
) -> bool:
    if not data.get("_cop_approved"):
        prompt_preview = request.prompt[:500]
        summary = (
            f"name={request.name}, profile={request.profile}, "
            f"schedule={request.schedule}, prompt={prompt_preview}"
        )
        allowed = await cop_gate_module.cop_gate(
            "create_periodic_agent",
            summary,
            data,
            source_group,
            deps,
        )
        if not allowed:
            return False
    return True


async def _create_periodic_agent(request: CreatePeriodicAgentRequest, deps: IpcDeps) -> None:
    setup = _periodic_agent_setup(request)
    if setup is None:
        return

    workspace_config.add_workspace_to_toml(
        request.name,
        WorkspaceConfig.model_validate({"profiles": [request.profile]}),
    )
    workspace_config.add_job_to_toml(
        request.name,
        JobConfig(
            workspace=request.name,
            schedule=request.schedule,
            prompt=request.prompt,
        ),
    )

    claude_md_path = setup.group_dir / "CLAUDE.md"
    if not claude_md_path.exists():
        claude_md_path.write_text(request.claude_md)

    channel = _command_center_channel(deps.channels(), setup.command_center)
    if channel is None:
        logger.warning(
            "Command center does not support create_group, periodic agent created without chat"
        )
        return

    agent_display_name = request.name.replace("-", " ").title()
    jid = _valid_jid(await channel.create_group(setup.chat_name))
    if jid is None:
        logger.warning(
            "Command center returned invalid jid for periodic agent",
            name=request.name,
            chat=setup.chat_name,
        )
        return

    profile = WorkspaceProfile(
        jid=jid,
        name=agent_display_name,
        folder=request.name,
        trigger="@pynchy",
        added_at=datetime.now(UTC).isoformat(),
    )
    deps.register_workspace(profile)

    task_id = f"periodic-{request.name}-{uuid.uuid4().hex[:8]}"

    await create_task(
        ScheduledTask(
            id=task_id,
            group_folder=request.name,
            chat_jid=jid,
            prompt=request.prompt,
            schedule_type="cron",
            schedule_value=request.schedule,
            context_mode="isolated",
            status="active",
            created_at=datetime.now(UTC).isoformat(),
        )
    )

    logger.info(
        "Periodic agent created via IPC",
        name=request.name,
        schedule=request.schedule,
        task_id=task_id,
        jid=jid,
    )


def _periodic_agent_setup(request: CreatePeriodicAgentRequest) -> _PeriodicAgentSetup | None:
    settings = get_settings()
    group_dir = settings.groups_dir / request.name
    group_dir.mkdir(parents=True, exist_ok=True)

    command_center = settings.command_center.connection
    if not command_center:
        logger.warning("create_periodic_agent requires command_center.connection")
        return None

    chat_name = request.chat or request.name
    return _PeriodicAgentSetup(
        settings=settings,
        command_center=command_center,
        group_dir=group_dir,
        chat_name=chat_name,
        chat_ref=ChatRefStr(f"{command_center}.chat.{chat_name}"),
    )


def _command_center_channel(
    channels: Sequence[object], command_center: str
) -> _CreateGroupChannel | None:
    return next(
        (
            cast("_CreateGroupChannel", channel)
            for channel in channels
            if getattr(channel, "name", None) == command_center and hasattr(channel, "create_group")
        ),
        None,
    )


def _valid_jid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


register("register_group", _handle_register_group)
register("create_periodic_agent", _handle_create_periodic_agent)
