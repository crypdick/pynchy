# Worktree Isolation

Workspaces whose profiles select `repo = "owner/repo"` get an isolated git
worktree instead of sharing a project checkout. Container execution mounts that
worktree at `/home/agent/src/<owner>/<repo>`. This stops concurrent agents
from editing the same files.

**Sync behavior:** Existing worktrees use best-effort `git fetch` + `git merge`, never `git reset --hard`. A service restart kills all running containers, so agents may leave uncommitted work in their worktree. That state is preserved and reported via system notices so the agent can resume cleanly.

**Virtual environment retention:** Pynchy records actual worktree use by updating
each `.venv` directory's modification time. The host Git sync removes root and
nested worktree virtual environments after 24 hours without use. Worktrees with
a running turn or live container session stay protected. This cleanup is scoped
to `data/worktrees/`; it never removes the host or project virtual environment.

**Retired thread cleanup:** When a provider reports that a dynamic thread was
deleted or archived, Pynchy stops its runtime and removes its worktree checkout,
session home, IPC files, generated environment, approvals, and group directory.
The Git branch remains, so reopening the thread can reattach committed work.
Dirty or untracked work blocks the entire cleanup and produces a warning instead
of losing unfinished work. Files outside the host-owned `logs/` directory in
the group workspace block cleanup the same way. Startup also reclaims orphaned
dynamic-thread artifacts left by an interrupted or older cleanup path.

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
includes every returned PR URL in the `Awaiting Review` outcome's evidence.
For Linear-owned work, successful publication attaches the host-validated PR
to the exact in-flight issue; the transition can idempotently backfill it from
preserved evidence. Returning a final response doesn't publish automatically.
