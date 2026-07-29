# Worktree Isolation

Workspaces whose profiles select `repo = "owner/repo"` get an isolated git
worktree instead of sharing a project checkout. Container execution mounts that
worktree at `/workspace/repos/<owner>/<repo>`. This stops concurrent agents
from editing the same files.

**Sync behavior:** Existing worktrees use best-effort `git fetch` + `git merge`, never `git reset --hard`. A service restart kills all running containers, so agents may leave uncommitted work in their worktree. That state is preserved and reported via system notices so the agent can resume cleanly.

## Routed Host Workspaces

Static direct-host workspaces, and direct-host workspaces without a selected
repository, run in their configured `cwd`. A routed direct-host conversation
with a selected repository instead runs in its stable child worktree under
`data/worktrees/<owner>/<repo>/<routed-folder>/`. Each child gets its own
`worktree/<routed-folder>` branch, so concurrent routes never edit or publish
the same branch.

Legacy commits or a pull request on a parent workspace stay separate from its
routed children. Pynchy never falls back to the parent branch to inspect or
publish a child's work. If a recovered routed host session has no child
worktree and its inherited parent checkout contains uncommitted work or commits
ahead of main, Pynchy stops before changing source. Parent work stays untouched;
recover or finish it from the parent workspace, then commit and publish each
child's work from that child's worktree.

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
For a routed host conversation, that pull request always comes from the
conversation's child branch, never its parent workspace branch.
The tool returns the canonical PR URL or an actionable failure. The agent
attaches every returned PR to the current Linear issue before moving it to
`Awaiting Review`. Returning a final response doesn't publish automatically.
