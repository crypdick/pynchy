# Memory and Sessions

## Memory System

- **Per-group memory**: Each group has a folder under `groups/{name}/` with its own `CLAUDE.md`. Mounted at `/workspace/group` inside the container.
- **Global memory**: `groups/global/CLAUDE.md` is mounted readonly at `/workspace/global` for non-god groups. Only writable from the god channel.
- **Project-access groups**: Groups with `project_access` get a worktree mount at `/workspace/project` instead of the global mount — they read `CLAUDE.md` from the project tree, not from `groups/global/`.
- **Files**: Groups can create/read files in their folder and reference them.
- The agent runs in `/workspace/group` (the container workdir). Session state (`.claude/`) is at `data/sessions/{group}/.claude/` on the host, mounted to `/home/agent/.claude` in the container.

## Session Management

- Each group maintains a conversation session (via Claude Agent SDK)
- Sessions auto-compact when context gets too long — this is a Claude Code SDK feature, not a Pynchy feature
- Session data is stored at `data/sessions/{group}/.claude/` and mounted into containers
