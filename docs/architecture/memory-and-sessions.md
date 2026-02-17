# Memory and Sessions

This page explains how Pynchy stores per-group memory and manages agent sessions. Understanding this helps you configure group memory, debug session issues, and reason about what context persists across conversations.

## Structured Memory (MCP Tools)

Agents have four MCP tools for persistent, searchable memory:

| Tool | Purpose |
|------|---------|
| `save_memory` | Store a fact with a key and content |
| `recall_memories` | Search memories by keyword (BM25 ranked) |
| `forget_memory` | Remove a memory by key |
| `list_memories` | List all saved memory keys |

Memories are **per-group isolated** — a memory saved by one group cannot be accessed by another. Each memory has a key (unique identifier), content, and a category:

| Category | Purpose |
|----------|---------|
| `core` | Permanent facts (default) — user preferences, project details |
| `daily` | Session context — ephemeral notes for the current work |
| `conversation` | Auto-archived conversation summaries from the PreCompact hook |

### How Search Works

`recall_memories` uses a two-tier search strategy:

1. **BM25 full-text search** — SQLite FTS5 tokenizes content and ranks results by term frequency. Best for natural language queries ("favorite color", "project deadline").
2. **LIKE fallback** — If FTS5 returns no results, falls back to substring matching. Catches queries that don't tokenize well (URLs, special characters, partial words).

### Storage

Memories live in `data/memories.db` — a dedicated SQLite database separate from the main `messages.db`. The memory plugin manages its own connection with WAL mode and mmap tuning.

## File-Based Memory

In addition to structured memory, agents have file-based storage:

- **Per-group memory** — Each group has a folder under `groups/{name}/` with its own `CLAUDE.md`, mounted at `/workspace/group` inside the container.
- **Global memory** — `groups/global/CLAUDE.md` mounts readonly at `/workspace/global` for non-god groups. Only the god channel can write to it.
- **Project-access groups** — Groups with `project_access` get a worktree mount at `/workspace/project` instead of the global mount. They read `CLAUDE.md` from the project tree, not from `groups/global/`.
- **Files** — Groups can create and read files in their folder and reference them in conversations.
- The agent runs in `/workspace/group` (the container workdir). Session state (`.claude/`) lives at `data/sessions/{group}/.claude/` on the host, mounted to `/home/agent/.claude` in the container.

### Conversation Archives

When a session compacts, the PreCompact hook archives the conversation transcript to both:
- `conversations/` folder in the group directory (markdown file, backward compatible)
- Structured memory with category `conversation` (searchable via `recall_memories`)

## Session Management

- Each group maintains a conversation session (via Claude Agent SDK)
- Sessions auto-compact when context grows too long — a Claude Code SDK feature, not a Pynchy feature
- Session data lives at `data/sessions/{group}/.claude/` and mounts into containers

## Plugin Architecture

Memory is a pluggable subsystem. The built-in `sqlite-memory` plugin provides the SQLite FTS5 backend, but the `pynchy_memory` hookspec allows alternative backends (e.g., PostgreSQL, JSONL). See [available plugins](../plugins/available.md) for the current list.
