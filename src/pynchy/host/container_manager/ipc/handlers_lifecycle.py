"""IPC handlers for session lifecycle: reset, finished_work, sync_worktree."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from pynchy.config import get_settings
from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_workspace_by_folder
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.ipc.write import write_ipc_response
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.host.git_ops import _worktree_merge, repo
from pynchy.host.git_ops._worktree_notify import host_notify_worktree_updates
from pynchy.host.git_ops.sync import host_sync_worktree
from pynchy.host.git_ops.sync_poll import needs_container_rebuild, needs_deploy
from pynchy.host.git_ops.utils import get_head_sha
from pynchy.logger import logger


def _sync_merge_and_check_deploy(
    source_group: str, repo_ctx: object
) -> tuple[dict[str, Any], str, bool | None]:
    """Synchronous git merge + deploy check — runs on a thread.

    Returns (merge_result, pre_merge_sha, deploy_info) where deploy_info
    is None if no deploy is needed, or a bool indicating whether a
    container rebuild is required.
    """
    typed_repo_ctx = cast("repo.RepoContext", repo_ctx)
    pre_merge_sha = get_head_sha(cwd=typed_repo_ctx.root)
    result = host_sync_worktree(source_group, typed_repo_ctx)

    deploy_info: bool | None = None
    if result.get("success"):
        post_merge_sha = get_head_sha(cwd=typed_repo_ctx.root)
        if pre_merge_sha not in {"unknown", post_merge_sha} and needs_deploy(
            pre_merge_sha, post_merge_sha
        ):
            deploy_info = needs_container_rebuild(pre_merge_sha, post_merge_sha)

    return result, pre_merge_sha, deploy_info


def _aggregate_sync_results(
    sync_results: list[tuple[Any, dict[str, Any], str, bool | None]],
) -> tuple[dict[str, Any], str, bool | None]:
    success = all(result.get("success") for _repo_ctx, result, _sha, _deploy in sync_results)
    deploy_item = next(
        (
            (pre_merge_sha, deploy_info)
            for _repo_ctx, _result, pre_merge_sha, deploy_info in sync_results
            if deploy_info is not None
        ),
        ("unknown", None),
    )
    return (
        {
            "success": success,
            "message": "All repo worktrees synced."
            if success
            else "One or more repo syncs failed.",
            "repos": {repo_ctx.slug: result for repo_ctx, result, _sha, _deploy in sync_results},
        },
        deploy_item[0],
        deploy_item[1],
    )


async def _handle_reset_context(
    data: dict[str, Any],
    source_group: str,
    _is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
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

    try:
        await _worktree_merge.merge_worktree_with_policy(group_folder)
    except Exception:  # noqa: BLE001, RUF100 - worktree sync failure must not block the reset_context boundary.
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
    _is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    chat_jid = data.get("chatJid", "")
    if not chat_jid:
        logger.warning("finished_work missing chatJid", source_group=source_group)
        return

    group = resolve_workspace_by_folder(source_group, deps)
    if group:
        _worktree_merge.background_merge_worktree(group)

    await deps.broadcast_host_message(
        chat_jid,
        "Scheduled task finished. Send a message to start a new conversation.",
    )
    logger.info("finished_work handled", group=source_group)


async def _handle_sync_worktree_to_main(
    data: dict[str, Any],
    source_group: str,
    _is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    request_id = data.get("request_id", "")

    if not data.get("_cop_approved"):
        summary = f"sync_worktree_to_main from '{source_group}'"
        allowed = await cop_gate_module.cop_gate(
            "sync_worktree_to_main",
            summary,
            data,
            source_group,
            deps,
            request_id=request_id,
        )
        if not allowed:
            return

    result_dir = get_settings().data_dir / "ipc" / source_group / "merge_results"

    repo_contexts = repo.resolve_repos_for_group(source_group)
    if not repo_contexts:
        write_ipc_response(
            result_dir / f"{request_id}.json",
            {"success": False, "message": "No repo configured for this group."},
        )
        logger.info("sync_worktree_to_main: no repo_ctx", group=source_group)
        return

    sync_results: list[tuple[Any, dict[str, Any], str, bool | None]] = []
    for repo_ctx in repo_contexts:
        repo_result, repo_pre_merge_sha, repo_deploy_info = await asyncio.to_thread(
            _sync_merge_and_check_deploy, source_group, repo_ctx
        )
        sync_results.append((repo_ctx, repo_result, repo_pre_merge_sha, repo_deploy_info))
    result, pre_merge_sha, deploy_info = _aggregate_sync_results(sync_results)
    write_ipc_response(result_dir / f"{request_id}.json", result)

    if result.get("success"):
        # IpcDeps satisfies WorktreeNotifyDeps directly.
        for repo_ctx, _repo_result, _repo_sha, _repo_deploy in sync_results:
            await host_notify_worktree_updates(source_group, deps, repo_ctx)

        if deploy_info is not None:
            await deps.trigger_deploy(pre_merge_sha, rebuild=deploy_info)

    logger.info(
        "sync_worktree_to_main handled",
        group=source_group,
        success=result.get("success"),
    )


register("reset_context", _handle_reset_context)
register("finished_work", _handle_finished_work)
register("sync_worktree_to_main", _handle_sync_worktree_to_main)
