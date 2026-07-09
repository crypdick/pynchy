# Worktree Isolation

Workspaces whose profiles select `repo = "owner/repo"` get their own git worktree mounted at `/workspace/repos/<owner>/<repo>` instead of mounting the shared project root. This stops concurrent containers from editing the same files.

**Sync behavior:** Existing worktrees use best-effort `git fetch` + `git merge`, never `git reset --hard`. A service restart kills all running containers, so agents may leave uncommitted work in their worktree. That state is preserved and reported via system notices so the agent can resume cleanly.

## Publishing

```toml
[profiles.code]
repo = "owner/repo"

[workspaces.code-improver]
profiles = ["code"]
```

The container-side tool (`sync_worktree_to_main`) commits through the host-side git workflow. Config no longer exposes a per-workspace git policy; repo-backed workspaces merge through the host.

**Post-run behavior** also follows the policy. After a container run:

- Worktree commits are rebased and merged into main, then pushed.
