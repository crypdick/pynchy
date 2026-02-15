# Architecture

Architecture decisions and design rationale.

## Philosophy

- **Simplicity** — The entire codebase should be something you can read and understand. A handful of source files. No microservices, no message queues, no abstraction layers.
- **Security through true isolation** — Agents run in Linux containers. Isolation is at the OS level, not application-level permission checks. See [Security Model](../security.md).
- **AI-native development** — No installation wizard, monitoring dashboard, or debugging tools. Claude Code guides setup, reads logs, and fixes problems.
- **Plugins over features** — Contributors write plugins, not PRs that add features to the base system.

## Topics

| Topic | What it covers |
|-------|---------------|
| [Container isolation](container-isolation.md) | Mounts, runtime detection, environment variables |
| [Message routing](message-routing.md) | Trigger patterns, routing behavior, transparent token stream |
| [Message types](message-types.md) | Type system, storage, SDK integration |
| [Memory and sessions](memory-and-sessions.md) | Per-group memory, global memory, session management |
| [Scheduled tasks](scheduled-tasks.md) | Task types, MCP tools, execution model |
| [Groups](groups.md) | Group management, god channel privileges |
| [Git sync](git-sync.md) | Coordinated worktree sync, host-mediated merges |

## Integration Points

| System | How it connects |
|--------|----------------|
| WhatsApp | neonize library for WhatsApp Web. Messages stored in SQLite, polled by router. QR code auth during setup. |
| Scheduler | Host-side scheduler spawns containers. `pynchy` MCP server (inside container) provides scheduling tools. Tasks stored in SQLite. |
| Web access | Built-in WebSearch and WebFetch tools via Claude Agent SDK. |
| Browser | agent-browser CLI with Chromium in container. Snapshot-based interaction. See `container/skills/agent-browser/`. |
