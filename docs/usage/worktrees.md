# Worktree Isolation

Non-admin groups with `pynchy_repo_access` (e.g. code-improver) get their own git worktree at `~/.config/pynchy/worktrees/{group}/` instead of mounting the shared project root. Stops concurrent containers from editing the same files.

**Sync behavior:** Existing worktrees use best-effort `git fetch` + `git merge`, never `git reset --hard`. A service restart kills all running containers, so agents may leave uncommitted work in their worktree. That state is preserved and reported via system notices so the agent can resume cleanly.

## Git Policy

Each workspace configures how committed changes get published via `git_policy` in `config.toml`:

| Policy | Behavior | Use Case |
|--------|----------|----------|
| `merge-to-main` (default) | Rebase onto main, ff-merge, push to origin | Trusted automation (code-improver, admin work) |
| `pull-request` | Push worktree branch to origin, open/update a PR | Review-gated workflows, experimental agents |

```toml
[workspaces.code-improver]
name = "Code Improver"
is_admin = false
repo_access = "owner/repo"
git_policy = "merge-to-main"   # default — changes land on main immediately

[workspaces.experimental-agent]
name = "Experimental Agent"
is_admin = false
repo_access = "owner/repo"
git_policy = "pull-request"    # changes go to a branch + PR for review
```

The container-side tool (`sync_worktree_to_main`) is the same for both policies. The host decides what "sync" means based on the workspace's `git_policy`. The response message tells the agent what happened (merged vs. PR URL).

**Post-run behavior** also follows the policy. After a container run:

- **merge-to-main:** Worktree commits are rebased and merged into main, then pushed.
- **pull-request:** Worktree branch gets pushed to origin and a PR is opened/updated. Main isn't touched, so other worktrees aren't notified.
