# Architecture

This section explains how Pynchy works under the hood. Understanding these concepts helps you troubleshoot issues, reason about security boundaries, and extend the system through plugins.

## Topics

| Topic | What it covers |
|-------|---------------|
| [Container isolation](container-isolation.md) | Mounts, runtime detection, environment variables |
| [IPC](ipc.md) | File-based communication between containers and host |
| [Message routing](message-routing.md) | Trigger patterns, routing behavior, transparent token stream |
| [Message types](message-types.md) | Type system, storage, SDK integration |
| [Memory and sessions](memory-and-sessions.md) | Per-group memory, global memory, session management |
| [Git sync](git-sync.md) | Coordinated worktree sync, host-mediated merges |
| [Security](security.md) | Trust model, security boundaries, credential handling |

## Integration Points

| System | How it connects |
|--------|----------------|
| Channels | Plugin-provided (WhatsApp, Slack, etc.). Messages stored in SQLite, routed to agents. See [available plugins](../plugins/available.md). |
| Scheduler | Host-side scheduler spawns containers. `pynchy` MCP server (inside container) provides scheduling tools. Tasks stored in SQLite. |
| Web access | Built-in WebSearch and WebFetch tools via Claude Agent SDK. |
| Browser | agent-browser CLI with Chromium in container. Snapshot-based interaction. See `container/skills/agent-browser/`. |
