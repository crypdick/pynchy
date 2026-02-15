# Coordinated Git Sync

Agents inside containers never push to main directly. The host mediates all merges into main, pushes to origin, and syncs other running agents.

## Design Principles

1. **Prefer mountable files over generated code** — Hook config and scripts live in `container/` as static files, mounted read-only. Don't generate complex logic in Python when a mountable file would do.
2. **Clear host/container naming** — Host-side functions prefixed `host_` (e.g., `host_sync_worktree()`). Container-side scripts live in `container/scripts/`.
3. **Self-contained error messages to containers** — Containers can't read host state (logs, config, etc.). Errors sent to containers must be descriptive and actionable. On conflict, leave the worktree in a resolvable state (conflict markers visible to agent) rather than aborting.
4. **Host owns main** — Agents never push to main directly. The host mediates all merges into main, pushes to origin, and syncs other agents.

For worktree isolation details, see `.claude/worktrees.md` in the project root.
