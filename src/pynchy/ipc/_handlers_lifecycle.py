"""IPC handlers for session lifecycle: reset, finished_work, sync_worktree."""

from __future__ import annotations

import json
from typing import Any

from pynchy.config import get_settings
from pynchy.git_ops.sync import (
    host_notify_worktree_updates,
    host_sync_worktree,
    needs_container_rebuild,
    needs_deploy,
    write_ipc_response,
)
from pynchy.git_ops.utils import get_head_sha
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import register
from pynchy.logger import logger
from pynchy.types import WorkspaceProfile


async def _handle_reset_context(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    chat_jid = data.get("chatJid", "")
    message = data.get("message", "")
    group_folder = data.get("groupFolder", source_group)

    if not chat_jid:
        logger.warning(
            "Invalid reset_context request: missing chatJid",
            source_group=source_group,
        )
        return

    import asyncio

    from pynchy.git_ops.worktree import merge_and_push_worktree

    logger.info(
        "Merging worktree before context reset",
        group=group_folder,
    )
    try:
        await asyncio.to_thread(merge_and_push_worktree, group_folder)
    except Exception as exc:
        logger.error(
            "Worktree merge failed during context reset",
            err=str(exc),
        )

    await deps.clear_session(group_folder)
    await deps.clear_chat_history(chat_jid)

    if message:
        reset_dir = get_settings().data_dir / "ipc" / group_folder
        reset_dir.mkdir(parents=True, exist_ok=True)
        reset_file = reset_dir / "reset_prompt.json"
        reset_file.write_text(
            json.dumps(
                {
                    "message": message,
                    "chatJid": chat_jid,
                    "needsDirtyRepoCheck": True,
                }
            )
        )

    deps.enqueue_message_check(chat_jid)
    logger.info(
        "Context reset via agent tool",
        group=group_folder,
    )


async def _handle_finished_work(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    chat_jid = data.get("chatJid", "")
    if not chat_jid:
        logger.warning("finished_work missing chatJid", source_group=source_group)
        return

    from pynchy.git_ops.worktree import background_merge_worktree

    group = next(
        (g for g in deps.workspaces().values() if g.folder == source_group),
        None,
    )
    if group:
        background_merge_worktree(group)

    await deps.broadcast_host_message(
        chat_jid,
        "Scheduled task finished. Send a message to start a new conversation.",
    )
    logger.info("finished_work handled", group=source_group)


async def _handle_sync_worktree_to_main(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    request_id = data.get("requestId", "")
    pre_merge_sha = get_head_sha()
    result = host_sync_worktree(source_group)

    result_dir = get_settings().data_dir / "ipc" / source_group / "merge_results"
    write_ipc_response(result_dir / f"{request_id}.json", result)

    if result.get("success"):
        post_merge_sha = get_head_sha()

        class _GitSyncAdapter:
            async def broadcast_host_message(self, jid: str, text: str) -> None:
                await deps.broadcast_host_message(jid, text)

            async def broadcast_system_notice(self, jid: str, text: str) -> None:
                await deps.broadcast_system_notice(jid, text)

            def workspaces(self) -> dict[str, WorkspaceProfile]:
                return deps.workspaces()

            async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
                pass  # adapter only used for worktree notifications

        await host_notify_worktree_updates(source_group, _GitSyncAdapter())

        if (
            pre_merge_sha != "unknown"
            and pre_merge_sha != post_merge_sha
            and needs_deploy(pre_merge_sha, post_merge_sha)
        ):
            rebuild = needs_container_rebuild(pre_merge_sha, post_merge_sha)
            await deps.trigger_deploy(pre_merge_sha, rebuild=rebuild)

    logger.info(
        "sync_worktree_to_main handled",
        group=source_group,
        success=result.get("success"),
    )


register("reset_context", _handle_reset_context)
register("finished_work", _handle_finished_work)
register("sync_worktree_to_main", _handle_sync_worktree_to_main)
