# Memory and Sessions

## Memory System

- **Per-group memory**: Each group has a folder with its own `CLAUDE.md` and `.claude/`.
- **Global memory**: Root `CLAUDE.md` and `.claude/` is read by all groups, but only writable from the god channel (self-chat).
- If an agent wants to edit global memory, it sends the request to the god container, which decides whether to approve it. This is mediated by a Deputy agent that blocks malicious requests.
- **Files**: Groups can create/read files in their folder and reference them.
- Agent runs in the group's folder and automatically inherits both CLAUDE.md files.

## Session Management

- Each group maintains a conversation session (via Claude Agent SDK)
- Sessions auto-compact when context gets too long, preserving critical information
- Session data is stored at `data/sessions/{group}/.claude/` and mounted into containers
