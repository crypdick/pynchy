"""Curated host-facing source-control API."""

from __future__ import annotations

from pynchy.host.git_ops import repo, sync_poll, worktree
from pynchy.host.git_ops._worktree_notify import (
    build_rebase_notice,
    host_notify_worktree_updates,
    last_notified_sha,
)
from pynchy.host.git_ops.personalization import sync_personalization_repo
from pynchy.host.git_ops.repo import (
    RepoContext,
    check_token_expiry,
    ensure_repo_cloned,
    get_repo_token,
    repo_container_path,
    repo_host_root,
    resolve_repos_for_group,
)
from pynchy.host.git_ops.sync import (
    GIT_POLICY_MERGE,
    GIT_POLICY_PR,
    GitSyncDeps,
    host_create_pr_from_worktree,
    host_sync_worktree,
    resolve_git_policy,
)
from pynchy.host.git_ops.sync_poll import (
    HostSyncState,
    _check_local_head_drift,
    _find_pynchy_repo_ctx,
    check_origin_drift,
    get_deploy_config_hash,
    get_local_head_sha,
    host_get_origin_main_sha,
    host_source_files_changed,
    host_update_main,
    host_update_main_result,
    needs_container_rebuild,
    needs_deploy,
    probe_origin_main_sha,
)
from pynchy.host.git_ops.utils import (
    count_commits,
    count_unpushed_commits,
    detect_main_branch,
    files_changed_between,
    get_head_commit_message,
    get_head_sha,
    git_env_with_token,
    is_repo_dirty,
    push_local_commits,
    redact_git_diagnostic,
    run_git,
)
from pynchy.host.git_ops.worktree import (
    WorktreeError,
    WorktreeResult,
    install_repo_hooks,
    reconcile_worktrees_at_startup,
)

check_local_head_drift = _check_local_head_drift
find_pynchy_repo_ctx = _find_pynchy_repo_ctx


def get_repo_context(slug: str) -> RepoContext | None:
    """Resolve a repository through the live source-control adapter."""
    return repo.get_repo_context(slug)


def ensure_worktree(group_folder: str, repo_ctx: RepoContext) -> WorktreeResult:
    """Provision a worktree through the live source-control adapter."""
    return worktree.ensure_worktree(group_folder, repo_ctx)


__all__ = [
    "GIT_POLICY_MERGE",
    "GIT_POLICY_PR",
    "GitSyncDeps",
    "HostSyncState",
    "RepoContext",
    "WorktreeError",
    "WorktreeResult",
    "build_rebase_notice",
    "check_local_head_drift",
    "check_origin_drift",
    "check_token_expiry",
    "count_commits",
    "count_unpushed_commits",
    "detect_main_branch",
    "ensure_repo_cloned",
    "ensure_worktree",
    "files_changed_between",
    "find_pynchy_repo_ctx",
    "get_deploy_config_hash",
    "get_head_commit_message",
    "get_head_sha",
    "get_local_head_sha",
    "get_repo_context",
    "get_repo_token",
    "git_env_with_token",
    "host_create_pr_from_worktree",
    "host_get_origin_main_sha",
    "host_notify_worktree_updates",
    "host_source_files_changed",
    "host_sync_worktree",
    "host_update_main",
    "host_update_main_result",
    "install_repo_hooks",
    "is_repo_dirty",
    "last_notified_sha",
    "needs_container_rebuild",
    "needs_deploy",
    "probe_origin_main_sha",
    "push_local_commits",
    "reconcile_worktrees_at_startup",
    "redact_git_diagnostic",
    "repo",
    "repo_container_path",
    "repo_host_root",
    "resolve_git_policy",
    "resolve_repos_for_group",
    "run_git",
    "sync_personalization_repo",
    "sync_poll",
    "worktree",
]
