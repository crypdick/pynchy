# Coordinated Git Sync

How Pynchy coordinates git operations between containers and the host. Use this page to configure profile `repo` access and troubleshoot merge conflicts in worktrees.

## Design Principles

1. **Prefer mountable files over generated code** — Hook config and scripts live in `src/pynchy/agent/` as static files, mounted read-only. Don't generate complex logic in Python when a mountable file suffices.
2. **Clear host/container naming** — Host-side functions use a `host_` prefix (e.g., `host_sync_worktree()`). Container-side scripts live in `src/pynchy/agent/scripts/`.
3. **Self-contained error messages to containers** — Containers can't read host state (logs, config, etc.). Errors sent to containers must include enough context to act on. On conflict, the host leaves the worktree in a resolvable state (conflict markers visible to agent) rather than aborting.
4. **Host owns main** — Agents never push to main directly. The host mediates all merges into main, pushes to origin, and syncs other agents.

For worktree isolation details, see `docs/usage/worktrees.md`.

## Change Detection

A background loop polls every 5 seconds and detects three types of drift:

| Drift type | What triggers it | Action |
|-----------|-----------------|--------|
| **Origin drift** | Remote main has new commits (e.g. pushed from another machine) | Offer the configured admin a fetch-and-upgrade action; with `scheduler.auto_deploy = true`, pull, notify running agents, and deploy eligible source changes |
| **Local HEAD drift** | Local HEAD differs from the SHA at last deploy (e.g. admin agent committed and pushed) | Offer the configured admin an upgrade action; with `scheduler.auto_deploy = true`, deploy eligible source changes |
| **Config drift** | `config.toml` or `litellm_config.yaml` hash changed | Trigger restart (no rebuild needed) |

Source-file changes (anything under `src/` or `pyproject.toml`) trigger a full deploy with container rebuild. Config-only changes trigger a lighter restart. `scheduler.auto_deploy` defaults to `false`; it only changes repository-revision updates, not direct local configuration changes.
