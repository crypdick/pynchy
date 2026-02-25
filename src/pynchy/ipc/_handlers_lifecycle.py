"""IPC handlers for session lifecycle: reset, finished_work, sync_worktree."""

from __future__ import annotations

import json
from typing import Any

from pynchy.config import get_settings
from pynchy.git_ops._worktree_notify import host_notify_worktree_updates
from pynchy.git_ops.sync import (
    GIT_POLICY_PR,
    host_create_pr_from_worktree,
    host_sync_worktree,
    resolve_git_policy,
)
from pynchy.git_ops.sync_poll import needs_container_rebuild, needs_deploy
from pynchy.git_ops.utils import get_head_sha
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import register
from pynchy.ipc._write import write_ipc_response
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

    from pynchy.git_ops.worktree import merge_worktree_with_policy

    try:
        await merge_worktree_with_policy(group_folder)
    except Exception as exc:
        logger.error("Worktree sync failed during context reset", err=str(exc))

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
    import asyncio

    from pynchy.git_ops.repo import resolve_repo_for_group

    request_id = data.get("requestId", "")
    result_dir = get_settings().data_dir / "ipc" / source_group / "merge_results"

    repo_ctx = resolve_repo_for_group(source_group)
    if repo_ctx is None:
        write_ipc_response(
            result_dir / f"{request_id}.json",
            {"success": False, "message": "No repo configured for this group."},
        )
        logger.info("sync_worktree_to_main: no repo_ctx", group=source_group)
        return

    policy = resolve_git_policy(source_group)

    if policy == GIT_POLICY_PR:
        result = await asyncio.to_thread(host_create_pr_from_worktree, source_group, repo_ctx)
        write_ipc_response(result_dir / f"{request_id}.json", result)
        # PR policy doesn't change main — no worktree notifications or deploy needed
    else:
        pre_merge_sha = get_head_sha(cwd=repo_ctx.root)
        result = host_sync_worktree(source_group, repo_ctx)
        write_ipc_response(result_dir / f"{request_id}.json", result)

        if result.get("success"):
            post_merge_sha = get_head_sha(cwd=repo_ctx.root)

            class _GitSyncAdapter:
                async def broadcast_host_message(self, jid: str, text: str) -> None:
                    await deps.broadcast_host_message(jid, text)

                async def broadcast_system_notice(self, jid: str, text: str) -> None:
                    await deps.broadcast_system_notice(jid, text)

                def has_active_session(self, group_folder: str) -> bool:
                    return deps.has_active_session(group_folder)

                def workspaces(self) -> dict[str, WorkspaceProfile]:
                    return deps.workspaces()

                async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
                    pass  # adapter only used for worktree notifications

            await host_notify_worktree_updates(source_group, _GitSyncAdapter(), repo_ctx)

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
        policy=policy,
        success=result.get("success"),
    )


register("reset_context", _handle_reset_context)
register("finished_work", _handle_finished_work)
register("sync_worktree_to_main", _handle_sync_worktree_to_main)
