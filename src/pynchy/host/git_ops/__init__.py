"""Git operations — sync, worktrees, and shared helpers."""

from pynchy.host.git_ops._worktree_notify import (
    WorktreeNotifyDeps,
    build_rebase_notice,
    host_notify_worktree_updates,
)
from pynchy.host.git_ops.sync_poll import (
    needs_container_rebuild,
    needs_deploy,
)
from pynchy.host.git_ops.utils import (
    count_commits,
    count_unpushed_commits,
    detect_main_branch,
    files_changed_between,
    get_head_sha,
    git_env_with_token,
    is_repo_dirty,
    push_local_commits,
    run_git,
)
from pynchy.host.git_ops.worktree import (
    WorktreeError,
    WorktreeResult,
    ensure_worktree,
    install_repo_hooks,
    reconcile_worktrees_at_startup,
)
from pynchy.host.git_ops.worktree_sync import GitSyncDeps

__all__ = [
    "GitSyncDeps",
    "WorktreeError",
    "WorktreeNotifyDeps",
    "WorktreeResult",
    "build_rebase_notice",
    "count_commits",
    "count_unpushed_commits",
    "detect_main_branch",
    "ensure_worktree",
    "files_changed_between",
    "get_head_sha",
    "git_env_with_token",
    "host_notify_worktree_updates",
    "install_repo_hooks",
    "is_repo_dirty",
    "needs_container_rebuild",
    "needs_deploy",
    "push_local_commits",
    "reconcile_worktrees_at_startup",
    "run_git",
]
