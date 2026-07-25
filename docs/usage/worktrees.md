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

The agent can call `sync_worktree_to_main` to publish committed changes for
review. Despite its legacy name, the agent-facing tool pushes the isolated
branch and opens or updates a pull request; it does not merge into `main`.
The tool returns the canonical PR URL or an actionable failure. The agent
attaches every returned PR to the current Linear issue before moving it to
`Awaiting Review`. Returning a final response doesn't publish automatically.
