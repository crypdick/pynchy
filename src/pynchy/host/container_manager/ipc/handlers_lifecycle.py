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
from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_workspace_by_folder
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.ipc.write import write_ipc_response
from pynchy.logger import logger


def _sync_merge_and_check_deploy(
    source_group: str, repo_ctx: Any
) -> tuple[dict[str, Any], str, bool | None]:
    """Synchronous git merge + deploy check — runs on a thread.

    Returns (merge_result, pre_merge_sha, deploy_info) where deploy_info
    is None if no deploy is needed, or a bool indicating whether a
    container rebuild is required.
    """
    pre_merge_sha = get_head_sha(cwd=repo_ctx.root)
    result = host_sync_worktree(source_group, repo_ctx)

    deploy_info: bool | None = None
    if result.get("success"):
        post_merge_sha = get_head_sha(cwd=repo_ctx.root)
        if (
            pre_merge_sha != "unknown"
            and pre_merge_sha != post_merge_sha
            and needs_deploy(pre_merge_sha, post_merge_sha)
        ):
            deploy_info = needs_container_rebuild(pre_merge_sha, post_merge_sha)

    return result, pre_merge_sha, deploy_info


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

    from pynchy.git_ops._worktree_merge import merge_worktree_with_policy

    try:
        await merge_worktree_with_policy(group_folder)
    except Exception:
        logger.exception("Worktree sync failed during context reset")

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

    from pynchy.git_ops._worktree_merge import background_merge_worktree

    group = resolve_workspace_by_folder(source_group, deps)
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

    if not data.get("_cop_approved"):
        from pynchy.host.container_manager.security.cop_gate import cop_gate

        summary = f"sync_worktree_to_main from '{source_group}'"
        allowed = await cop_gate(
            "sync_worktree_to_main",
            summary,
            data,
            source_group,
            deps,
            request_id=data.get("requestId"),
        )
        if not allowed:
            return

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
        # Run blocking git operations (fetch, merge, push, diff) on a thread
        # to avoid blocking the event loop — same pattern as the PR path above.
        result, pre_merge_sha, deploy_info = await asyncio.to_thread(
            _sync_merge_and_check_deploy, source_group, repo_ctx
        )
        write_ipc_response(result_dir / f"{request_id}.json", result)

        if result.get("success"):
            # IpcDeps satisfies WorktreeNotifyDeps directly — no adapter needed.
            await host_notify_worktree_updates(source_group, deps, repo_ctx)

            if deploy_info is not None:
                await deps.trigger_deploy(pre_merge_sha, rebuild=deploy_info)

    logger.info(
        "sync_worktree_to_main handled",
        group=source_group,
        policy=policy,
        success=result.get("success"),
    )


register("reset_context", _handle_reset_context)
register("finished_work", _handle_finished_work)
register("sync_worktree_to_main", _handle_sync_worktree_to_main)
