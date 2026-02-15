# Worktree Isolation

Non-god groups with `project_access` (e.g. code-improver) get their own git worktree at `~/.config/pynchy/worktrees/{group}/` instead of mounting the shared project root. This prevents concurrent containers from editing the same files.

**Sync behavior:** Existing worktrees use best-effort `git fetch` + `git merge`, never `git reset --hard`. A service restart kills all running containers, so agents may leave uncommitted work in their worktree. That state is preserved and reported via system notices so the agent can resume gracefully.

**Post-run merge:** After a successful container run, worktree commits are fast-forward merged into the main branch and pushed. Non-fast-forward merges are logged but not forced.
