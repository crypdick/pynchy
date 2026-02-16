# Memory and Sessions

This page explains how Pynchy stores per-group memory and manages agent sessions. Understanding this helps you configure group memory, debug session issues, and reason about what context persists across conversations.

## Memory System

- **Per-group memory** — Each group has a folder under `groups/{name}/` with its own `CLAUDE.md`, mounted at `/workspace/group` inside the container.
- **Global memory** — `groups/global/CLAUDE.md` mounts readonly at `/workspace/global` for non-god groups. Only the god channel can write to it.
- **Project-access groups** — Groups with `project_access` get a worktree mount at `/workspace/project` instead of the global mount. They read `CLAUDE.md` from the project tree, not from `groups/global/`.
- **Files** — Groups can create and read files in their folder and reference them in conversations.
- The agent runs in `/workspace/group` (the container workdir). Session state (`.claude/`) lives at `data/sessions/{group}/.claude/` on the host, mounted to `/home/agent/.claude` in the container.

## Session Management

- Each group maintains a conversation session (via Claude Agent SDK)
- Sessions auto-compact when context grows too long — a Claude Code SDK feature, not a Pynchy feature
- Session data lives at `data/sessions/{group}/.claude/` and mounts into containers
