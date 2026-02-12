"""File-based IPC watcher.

Port of src/ipc.ts — async polling loop that processes IPC files from containers.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.config import (
    ASSISTANT_NAME,
    DATA_DIR,
    IPC_POLL_INTERVAL,
    MAIN_GROUP_FOLDER,
    PROJECT_ROOT,
    TIMEZONE,
)
from pynchy.db import create_task, delete_task, get_task_by_id, update_task
from pynchy.deploy import finalize_deploy
from pynchy.logger import logger
from pynchy.types import ContainerConfig, RegisteredGroup


class IpcDeps(Protocol):
    """Dependencies for IPC processing."""

    async def send_message(self, jid: str, text: str) -> None: ...

    def registered_groups(self) -> dict[str, RegisteredGroup]: ...

    def register_group(self, jid: str, group: RegisteredGroup) -> None: ...

    async def sync_group_metadata(self, force: bool) -> None: ...

    async def get_available_groups(self) -> list[Any]: ...

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_main: bool,
        available_groups: list[Any],
        registered_jids: set[str],
    ) -> None: ...

    async def clear_session(self, group_folder: str) -> None: ...

    def enqueue_message_check(self, group_jid: str) -> None: ...


_ipc_watcher_running = False


async def start_ipc_watcher(deps: IpcDeps) -> None:
    """Start the IPC watcher polling loop."""
    global _ipc_watcher_running
    if _ipc_watcher_running:
        logger.debug("IPC watcher already running, skipping duplicate start")
        return
    _ipc_watcher_running = True

    ipc_base_dir = DATA_DIR / "ipc"
    ipc_base_dir.mkdir(parents=True, exist_ok=True)

    async def process_ipc_files() -> None:
        try:
            group_folders = [
                f.name for f in ipc_base_dir.iterdir() if f.is_dir() and f.name != "errors"
            ]
        except Exception as exc:
            logger.error("Error reading IPC base directory", err=str(exc))
            await asyncio.sleep(IPC_POLL_INTERVAL)
            return

        registered_groups = deps.registered_groups()

        for source_group in group_folders:
            is_main = source_group == MAIN_GROUP_FOLDER
            messages_dir = ipc_base_dir / source_group / "messages"
            tasks_dir = ipc_base_dir / source_group / "tasks"

            # Process messages
            try:
                if messages_dir.exists():
                    message_files = sorted(f for f in messages_dir.iterdir() if f.suffix == ".json")
                    for file_path in message_files:
                        try:
                            data = json.loads(file_path.read_text())
                            if (
                                data.get("type") == "message"
                                and data.get("chatJid")
                                and data.get("text")
                            ):
                                target_group = registered_groups.get(data["chatJid"])
                                if is_main or (
                                    target_group and target_group.folder == source_group
                                ):
                                    await deps.send_message(
                                        data["chatJid"],
                                        f"{ASSISTANT_NAME}: {data['text']}",
                                    )
                                    logger.info(
                                        "IPC message sent",
                                        chat_jid=data["chatJid"],
                                        source_group=source_group,
                                    )
                                else:
                                    logger.warning(
                                        "Unauthorized IPC message attempt blocked",
                                        chat_jid=data["chatJid"],
                                        source_group=source_group,
                                    )
                            file_path.unlink()
                        except Exception as exc:
                            logger.error(
                                "Error processing IPC message",
                                file=file_path.name,
                                source_group=source_group,
                                err=str(exc),
                            )
                            error_dir = ipc_base_dir / "errors"
                            error_dir.mkdir(parents=True, exist_ok=True)
                            file_path.rename(error_dir / f"{source_group}-{file_path.name}")
            except Exception as exc:
                logger.error(
                    "Error reading IPC messages directory",
                    err=str(exc),
                    source_group=source_group,
                )

            # Process tasks
            try:
                if tasks_dir.exists():
                    task_files = sorted(f for f in tasks_dir.iterdir() if f.suffix == ".json")
                    for file_path in task_files:
                        try:
                            data = json.loads(file_path.read_text())
                            await process_task_ipc(data, source_group, is_main, deps)
                            file_path.unlink()
                        except Exception as exc:
                            logger.error(
                                "Error processing IPC task",
                                file=file_path.name,
                                source_group=source_group,
                                err=str(exc),
                            )
                            error_dir = ipc_base_dir / "errors"
                            error_dir.mkdir(parents=True, exist_ok=True)
                            file_path.rename(error_dir / f"{source_group}-{file_path.name}")
            except Exception as exc:
                logger.error(
                    "Error reading IPC tasks directory",
                    err=str(exc),
                    source_group=source_group,
                )

    while True:
        await process_ipc_files()
        await asyncio.sleep(IPC_POLL_INTERVAL)


async def process_task_ipc(
    data: dict[str, Any],
    source_group: str,
    is_main: bool,
    deps: IpcDeps,
) -> None:
    """Process a single IPC task command."""
    registered_groups = deps.registered_groups()

    match data.get("type"):
        case "schedule_task":
            prompt = data.get("prompt")
            schedule_type = data.get("schedule_type")
            schedule_value = data.get("schedule_value")
            target_jid = data.get("targetJid")

            if not (prompt and schedule_type and schedule_value and target_jid):
                return

            target_group_entry = registered_groups.get(target_jid)
            if not target_group_entry:
                logger.warning(
                    "Cannot schedule task: target group not registered",
                    target_jid=target_jid,
                )
                return

            target_folder = target_group_entry.folder

            # Authorization: non-main groups can only schedule for themselves
            if not is_main and target_folder != source_group:
                logger.warning(
                    "Unauthorized schedule_task attempt blocked",
                    source_group=source_group,
                    target_folder=target_folder,
                )
                return

            next_run: str | None = None
            if schedule_type == "cron":
                try:
                    tz = ZoneInfo(TIMEZONE)
                    cron = croniter(schedule_value, datetime.now(tz))
                    next_run = cron.get_next(datetime).isoformat()
                except (ValueError, KeyError):
                    logger.warning(
                        "Invalid cron expression",
                        schedule_value=schedule_value,
                    )
                    return
            elif schedule_type == "interval":
                try:
                    ms = int(schedule_value)
                    if ms <= 0:
                        raise ValueError("Interval must be positive")
                except (ValueError, TypeError):
                    logger.warning("Invalid interval", schedule_value=schedule_value)
                    return
                next_run = datetime.fromtimestamp(
                    datetime.now(UTC).timestamp() + ms / 1000,
                    tz=UTC,
                ).isoformat()
            elif schedule_type == "once":
                try:
                    scheduled = datetime.fromisoformat(schedule_value)
                    next_run = scheduled.isoformat()
                except (ValueError, TypeError):
                    logger.warning("Invalid timestamp", schedule_value=schedule_value)
                    return

            task_id = f"task-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
            context_mode = data.get("context_mode")
            if context_mode not in ("group", "isolated"):
                context_mode = "isolated"

            await create_task(
                {
                    "id": task_id,
                    "group_folder": target_folder,
                    "chat_jid": target_jid,
                    "prompt": prompt,
                    "schedule_type": schedule_type,
                    "schedule_value": schedule_value,
                    "context_mode": context_mode,
                    "next_run": next_run,
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            logger.info(
                "Task created via IPC",
                task_id=task_id,
                source_group=source_group,
                target_folder=target_folder,
                context_mode=context_mode,
            )

        case "pause_task":
            task_id = data.get("taskId")
            if task_id:
                task = await get_task_by_id(task_id)
                if task and (is_main or task.group_folder == source_group):
                    await update_task(task_id, {"status": "paused"})
                    logger.info(
                        "Task paused via IPC",
                        task_id=task_id,
                        source_group=source_group,
                    )
                else:
                    logger.warning(
                        "Unauthorized task pause attempt",
                        task_id=task_id,
                        source_group=source_group,
                    )

        case "resume_task":
            task_id = data.get("taskId")
            if task_id:
                task = await get_task_by_id(task_id)
                if task and (is_main or task.group_folder == source_group):
                    await update_task(task_id, {"status": "active"})
                    logger.info(
                        "Task resumed via IPC",
                        task_id=task_id,
                        source_group=source_group,
                    )
                else:
                    logger.warning(
                        "Unauthorized task resume attempt",
                        task_id=task_id,
                        source_group=source_group,
                    )

        case "cancel_task":
            task_id = data.get("taskId")
            if task_id:
                task = await get_task_by_id(task_id)
                if task and (is_main or task.group_folder == source_group):
                    await delete_task(task_id)
                    logger.info(
                        "Task cancelled via IPC",
                        task_id=task_id,
                        source_group=source_group,
                    )
                else:
                    logger.warning(
                        "Unauthorized task cancel attempt",
                        task_id=task_id,
                        source_group=source_group,
                    )

        case "refresh_groups":
            if is_main:
                logger.info(
                    "Group metadata refresh requested via IPC",
                    source_group=source_group,
                )
                await deps.sync_group_metadata(True)
                available_groups = await deps.get_available_groups()
                deps.write_groups_snapshot(
                    source_group,
                    True,
                    available_groups,
                    set(registered_groups.keys()),
                )
            else:
                logger.warning(
                    "Unauthorized refresh_groups attempt blocked",
                    source_group=source_group,
                )

        case "deploy":
            if not is_main:
                logger.warning(
                    "Unauthorized deploy attempt",
                    source_group=source_group,
                )
                return
            await _handle_deploy(data, source_group, deps)

        case "reset_context":
            chat_jid = data.get("chatJid", "")
            message = data.get("message", "")
            group_folder = data.get("groupFolder", source_group)

            if not chat_jid or not message:
                logger.warning(
                    "Invalid reset_context request",
                    source_group=source_group,
                )
                return

            await deps.clear_session(group_folder)

            # Write reset prompt for _process_group_messages to pick up
            reset_dir = DATA_DIR / "ipc" / group_folder
            reset_dir.mkdir(parents=True, exist_ok=True)
            reset_file = reset_dir / "reset_prompt.json"
            reset_file.write_text(json.dumps({"message": message, "chatJid": chat_jid}))

            deps.enqueue_message_check(chat_jid)
            logger.info(
                "Context reset via agent tool",
                group=group_folder,
            )

        case "register_group":
            if not is_main:
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
                deps.register_group(
                    jid,
                    RegisteredGroup(
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

        case _:
            logger.warning("Unknown IPC task type", type=data.get("type"))


async def _handle_deploy(
    data: dict[str, Any],
    source_group: str,
    deps: IpcDeps,
) -> None:
    """Handle a deploy request from the main group agent.

    The agent is responsible for git add/commit before calling deploy.
    This handler reads the current HEAD (for rollback), optionally rebuilds
    the container, writes a continuation file, and SIGTERMs the process.
    """
    rebuild_container = data.get("rebuildContainer", False)
    resume_prompt = data.get(
        "resumePrompt",
        "Deploy complete. Verifying service health.",
    )
    head_sha = data.get("headSha", "")
    session_id = data.get("sessionId", "")
    chat_jid = data.get("chatJid", "")

    if not chat_jid:
        logger.error("Deploy request missing chatJid")
        return

    # 1. Optional container rebuild
    if rebuild_container:
        build_script = PROJECT_ROOT / "container" / "build.sh"
        if build_script.exists():
            logger.info("Rebuilding container image...")
            result = subprocess.run(
                [str(build_script)],
                cwd=str(PROJECT_ROOT / "container"),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                await _deploy_error(
                    deps,
                    chat_jid,
                    f"Container rebuild failed: {result.stderr[-500:]}",
                )
                return
        else:
            logger.warning(
                "rebuild_container requested but build.sh not found",
            )

    # 2. Write continuation, notify WhatsApp, and SIGTERM
    await finalize_deploy(
        send_message=deps.send_message,
        chat_jid=chat_jid,
        commit_sha=head_sha,
        previous_sha=head_sha,
        session_id=session_id,
        resume_prompt=resume_prompt,
    )


async def _deploy_error(
    deps: IpcDeps,
    chat_jid: str,
    message: str,
) -> None:
    """Send a deploy error message back to the main group."""
    logger.error("Deploy failed", error=message)
    await deps.send_message(
        chat_jid,
        f"{ASSISTANT_NAME}: Deploy failed: {message}",
    )
