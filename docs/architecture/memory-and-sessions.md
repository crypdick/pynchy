# Memory and Sessions — Architecture

Internal design of the memory subsystem and session management. For user-facing memory docs (tools, categories, file-based memory), see [Usage — Memory](../usage/memory.md).

## Memory Plugin Architecture

Memory is pluggable via the `pynchy_memory` hookspec. Any plugin implementing this hook can provide an alternative memory backend.

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

## Obsidian Learning

Automatic learning can mount an Obsidian vault root into agent containers and run a hidden reviewer after successful turns. The configured vault root mounts read-write at `/workspace/vault` by default and acts as the global memory namespace.

The reviewer receives a bounded packet from the completed turn, not the full transcript. It writes immediately when the packet contains durable learning, and it should use the vault's existing folder organization before falling back to profile-scoped paths. Memory notes rely on folder placement, not semantic frontmatter.

Profile fallback paths use the active workspace profile name, or `default` when no profile is configured:

| Purpose | Vault path |
|---------|------------|
| Fallback memory notes | `systems/pynchy/profiles/{profile}/memory` |
| Learned skills | `systems/pynchy/profiles/{profile}/skills` |

Learned skills live under `systems/pynchy/profiles/{profile}/skills/<skill-name>/SKILL.md` and use the existing Pynchy skill format. Skill activation still follows workspace skill selection. Configure `skills = ["learned"]` or `skills = ["*"]` for a profile or workspace to copy learned skills into future sessions.

Learning packets live in a durable filesystem queue under `data/ipc/learning`. The queue uses pending, claimed, done, and error states so work can survive process restarts and another worker can reclaim expired jobs.

V1 learning deliberately mounts the configured vault root as a broad namespace. Access controls and narrower subdirectory mounts belong to a later policy layer.

## Session Management

- Each group maintains a conversation session via the agent core SDK
- Sessions auto-compact when context grows too long (an SDK feature, not Pynchy's)
- Session data lives at `data/sessions/{group}/.claude/` on the host, mounted into containers at `/home/agent/.claude`
- The PreCompact hook archives conversation transcripts before compaction (see [Usage — Memory § Conversation Archives](../usage/memory.md#conversation-archives))

A persisted session means Pynchy can rehydrate that conversation; it does not mean an agent
currently runs. The [interrupted turn recovery](message-routing.md#interrupted-turn-recovery)
ledger tracks running work separately so restarts resume unfinished turns without waking idle
conversations.

---

**Want to customize this?** Write your own memory backend plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
