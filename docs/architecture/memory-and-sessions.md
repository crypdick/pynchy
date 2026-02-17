# Memory and Sessions — Architecture

This page covers the internal design of the memory subsystem and session management. For user-facing memory documentation (tools, categories, file-based memory), see [Usage — Memory](../usage/memory.md).

## Memory Plugin Architecture

Memory is a pluggable subsystem defined by the `pynchy_memory` hookspec. Any plugin implementing this hook can provide an alternative memory backend.

**Hookspec contract:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `save` | `(group_folder, key, content, category, metadata) → dict` | Store a memory |
| `recall` | `(group_folder, query, category, limit) → list[dict]` | Search memories |
| `forget` | `(group_folder, key) → dict` | Remove a memory |
| `list_keys` | `(group_folder, category) → list[dict]` | List memory keys |
| `init` | `() → coroutine` | Async setup (create tables, connections) |
| `close` | `() → coroutine` | Async teardown |

### Built-in: sqlite-memory

The default backend uses SQLite FTS5 for full-text search with BM25 ranking, falling back to LIKE substring matching when FTS returns no results.

**Storage:** Dedicated `data/memories.db` database (separate from `messages.db`). Uses WAL mode and mmap tuning for concurrent access.

**Search pipeline:** Query → FTS5 tokenization → BM25 ranking → results. If empty → LIKE fallback → results.

## Session Management

- Each group maintains a conversation session via the agent core SDK
- Sessions auto-compact when context grows too long (an SDK feature, not Pynchy's)
- Session data lives at `data/sessions/{group}/.claude/` on the host, mounted into containers at `/home/agent/.claude`
- The PreCompact hook archives conversation transcripts before compaction (see [Usage — Memory § Conversation Archives](../usage/memory.md#conversation-archives))

---

**Want to customize this?** Write your own memory backend plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
