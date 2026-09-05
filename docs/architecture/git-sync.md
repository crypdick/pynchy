# Coordinated Git Sync

How Pynchy coordinates git operations between containers and the host. Use this
page to configure profile `repo` access and troubleshoot publication conflicts
in worktrees.

## Design Principles

1. **Prefer mountable files over generated code** — Hook config and scripts live in `src/pynchy/agent/` as static files, mounted read-only. Don't generate complex logic in Python when a mountable file suffices.
2. **Clear host/container naming** — Host-side functions use a `host_` prefix (e.g., `host_create_pr_from_worktree()`). Container-side scripts live in `src/pynchy/agent/scripts/`.
3. **Self-contained error messages to containers** — Containers can't read host state (logs, config, etc.). Errors sent to containers must include enough context to act on. On conflict, the host leaves the worktree in a resolvable state (conflict markers visible to agent) rather than aborting.
4. **Host owns publication credentials** — Agents never push directly. The host
   publishes isolated worktree branches as pull requests and can publish the
   canonical personalization checkout through its fixed operator command.
   Merged remote changes return through the normal origin-drift path.

For worktree isolation details, see `docs/usage/worktrees.md`.

## Change Detection

A background loop polls every 5 seconds and detects three types of drift:

| Drift type | What triggers it | Action |
|-----------|-----------------|--------|
| **Origin drift** | Remote main has new commits (e.g. pushed from another machine) | Offer the configured admin a fetch-and-upgrade action; with `scheduler.auto_deploy = true`, pull, notify running agents, and deploy eligible source changes |
| **Local HEAD drift** | Local HEAD differs from the SHA at last deploy (e.g. admin agent committed and pushed) | Offer the configured admin an upgrade action; with `scheduler.auto_deploy = true`, deploy eligible source changes |
| **Config drift** | `.env`, restart-sensitive layered `pynchy.toml` fields, or `litellm.yaml` changed | Trigger restart (no rebuild needed) |
| **Automation drift** | A file-backed automation definition or referenced prompt changed | Reconcile configured tasks and Temporal schedules without restarting |

Source-file changes (anything under `src/` or `pyproject.toml`) trigger a full
deploy with container rebuild. Restart-sensitive config changes trigger a
lighter restart. Valid automation changes reconcile configured task rows, host
cron snapshots, and Temporal schedules after startup recovery completes.
Invalid or incomplete edits keep the previous runtime snapshot and retry on a
later poll. Personalized skills refresh into session registries before the next
turn.
`scheduler.auto_deploy` defaults to `false`; it only changes
repository-revision updates, not direct local configuration changes.

The sync loop validates, commits, and pushes local changes in the independent
personalization repository. The host operator invokes that same fixed-target
operation with `pynchy publish-personalization`; it accepts no repository,
branch, or remote override. Invalid in-progress edits remain uncommitted and
are retried on a later poll. See
[Personalization repository](../usage/personalization.md) for operator steps
and divergence behavior.
