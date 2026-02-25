"""Git operations — sync, worktrees, and shared helpers."""

from pynchy.git_ops._worktree_notify import host_notify_worktree_updates
from pynchy.git_ops.sync import (
    GitSyncDeps,
    host_sync_worktree,
)
from pynchy.git_ops.sync_poll import (
    needs_container_rebuild,
    needs_deploy,
    start_host_git_sync_loop,
)
from pynchy.git_ops.utils import (
    GitCommandError,
    count_unpushed_commits,
    detect_main_branch,
    files_changed_between,
    get_head_sha,
    git_env_with_token,
    is_repo_dirty,
    push_local_commits,
    require_success,
    run_git,
)
from pynchy.git_ops.worktree import (
    WorktreeError,
    WorktreeResult,
    ensure_worktree,
    install_pre_commit_hooks,
    merge_and_push_worktree,
    merge_worktree,
    merge_worktree_with_policy,
    reconcile_worktrees_at_startup,
)

__all__ = [
    "GitCommandError",
    "GitSyncDeps",
    "WorktreeError",
    "WorktreeResult",
    "count_unpushed_commits",
    "detect_main_branch",
    "ensure_worktree",
    "files_changed_between",
    "get_head_sha",
    "git_env_with_token",
    "host_notify_worktree_updates",
    "host_sync_worktree",
    "install_pre_commit_hooks",
    "is_repo_dirty",
    "merge_and_push_worktree",
    "merge_worktree",
    "merge_worktree_with_policy",
    "needs_container_rebuild",
    "needs_deploy",
    "push_local_commits",
    "reconcile_worktrees_at_startup",
    "require_success",
    "run_git",
    "start_host_git_sync_loop",
]
