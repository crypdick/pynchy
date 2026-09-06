"""Curated host-facing source-control API."""

from __future__ import annotations

from collections.abc import (
    Sequence,
)
from pathlib import Path

from pynchy.host.git_ops import _routed_host_worktree, repo, sync_poll, worktree
from pynchy.host.git_ops._worktree_models import (
    RoutedHostWorktreeError,
    RoutedHostWorktreeResult,
)
from pynchy.host.git_ops._worktree_notify import (
    build_rebase_notice,
    host_notify_worktree_updates,
    last_notified_sha,
)
from pynchy.host.git_ops.managed_feature import (
    ManagedFeaturePublication,
    ManagedFeatureResolution,
    host_rebase_managed_feature,  # noqa: F401 - curated Git adapter surface.
    read_managed_feature_patch,
    resolve_managed_feature_publication,
)
from pynchy.host.git_ops.personalization import sync_personalization_repo
from pynchy.host.git_ops.repo import (
    RepoContext,
    RepoSettings,
    check_token_expiry,
    configure_repo_runtime,
    ensure_repo_cloned,
    get_repo_token,
    repo_container_path,
    repo_host_root,
    resolve_repos_for_group,
)
from pynchy.host.git_ops.sync import (
    host_create_pr_from_managed_feature,
    host_create_pr_from_worktree,
)
from pynchy.host.git_ops.sync_poll import (
    GitSyncRuntime,
    HostSyncState,
    _check_local_head_drift,
    _find_pynchy_repo_ctx,
    check_origin_drift,
    configure_git_sync_runtime,
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
from pynchy.host.git_ops.worktree_sync import GitSyncDeps
from pynchy.host.git_ops.worktree_venv import mark_worktree_used, prune_stale_worktree_venvs

check_local_head_drift = _check_local_head_drift
find_pynchy_repo_ctx = _find_pynchy_repo_ctx


def get_repo_context(slug: str) -> RepoContext | None:
    """Resolve a repository through the live source-control adapter."""
    return repo.get_repo_context(slug)


def ensure_worktree(group_folder: str, repo_ctx: RepoContext) -> WorktreeResult:
    """Provision a worktree through the live source-control adapter."""
    return worktree.ensure_worktree(group_folder, repo_ctx)


def select_routed_host_repo(source_cwd: Path, repo_contexts: Sequence[RepoContext]) -> RepoContext:
    """Identify the configured repository that owns a routed host CWD."""
    return _routed_host_worktree.select_routed_host_repo(source_cwd, repo_contexts)


def resolve_routed_host_worktree_cwd(
    group_folder: str,
    source_cwd: Path,
    repo_contexts: Sequence[RepoContext],
    *,
    recovered: bool,
) -> RoutedHostWorktreeResult:
    """Resolve one routed host conversation to its isolated worktree CWD."""
    return worktree.resolve_routed_host_worktree_cwd(
        group_folder,
        source_cwd,
        repo_contexts,
        recovered=recovered,
    )


__all__ = [
    "GitSyncDeps",
    "GitSyncRuntime",
    "HostSyncState",
    "ManagedFeaturePublication",
    "ManagedFeatureResolution",
    "RepoContext",
    "RepoSettings",
    "RoutedHostWorktreeError",
    "RoutedHostWorktreeResult",
    "WorktreeError",
    "WorktreeResult",
    "build_rebase_notice",
    "check_local_head_drift",
    "check_origin_drift",
    "check_token_expiry",
    "configure_git_sync_runtime",
    "configure_repo_runtime",
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
    "host_create_pr_from_managed_feature",
    "host_create_pr_from_worktree",
    "host_get_origin_main_sha",
    "host_notify_worktree_updates",
    "host_source_files_changed",
    "host_update_main",
    "host_update_main_result",
    "install_repo_hooks",
    "is_repo_dirty",
    "last_notified_sha",
    "mark_worktree_used",
    "needs_container_rebuild",
    "needs_deploy",
    "probe_origin_main_sha",
    "prune_stale_worktree_venvs",
    "push_local_commits",
    "read_managed_feature_patch",
    "reconcile_worktrees_at_startup",
    "redact_git_diagnostic",
    "repo",
    "repo_container_path",
    "repo_host_root",
    "resolve_managed_feature_publication",
    "resolve_repos_for_group",
    "resolve_routed_host_worktree_cwd",
    "run_git",
    "select_routed_host_repo",
    "sync_personalization_repo",
    "sync_poll",
    "worktree",
]
