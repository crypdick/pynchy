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
| Learned skills | `systems/pynchy/skills` |

Learned skills live in the shared registry at `systems/pynchy/skills/<skill-name>/SKILL.md` and use the existing Pynchy skill format. Profiles determine which skills their workspaces may receive through `skills`; exact names are the preferred allowlist, while `learned` and `*` select by tier. `denied_skills` always blocks a named skill. A session can discover the global catalog with `search_skills` and request a one-time or persistent grant with `request_skill_access`; persistent decisions update the workspace profile and are synchronized into the next session registry. The selection refreshes for cold containers, warm-container follow-up turns, and direct-host turns.

Learning packets live in a durable filesystem queue under `data/ipc/learning`. The queue uses pending, claimed, done, and error states so work can survive process restarts and another worker can reclaim expired jobs.

V1 learning deliberately mounts the configured vault root as a broad namespace. Access controls and narrower subdirectory mounts belong to a later policy layer.

## Session Management

- Each visible Discord thread owns exactly one durable runtime: workspace,
  worktree, provider session, queue, and checkpoint ledger.
- Interactive and scheduled turns in that thread use the same agent-core
  session. Worker processes are disposable and can resume the stored session.
- Scheduled tasks either continue the current session or visibly reset it
  before an occurrence. Pynchy doesn't support ephemeral scheduled sessions.
- Sessions auto-compact when context grows too long (an SDK feature, not Pynchy's)
- Session data lives under `data/sessions/{group}/` on the host. Pynchy mounts
  its scoped `.claude/` and `.codex/` homes at `/home/agent/.claude` and
  `/home/agent/.codex` respectively.
- Direct-host Codex workspaces use `data/sessions/{group}/.codex/` as their scoped Codex home. This gives them the same injected skill registry and Pynchy MCP tools as container sessions, while preserving host execution.
- Public-source and secret-source security taint is sticky for the lifetime of
  the durable session. Continuation can't clear it; the unified context reset
  clears both the provider session and its persisted taint.
- The PreCompact hook archives conversation transcripts before compaction (see [Usage — Memory § Conversation Archives](../usage/memory.md#conversation-archives))

A persisted session means Pynchy can rehydrate that conversation; it does not mean an agent
currently runs. The [interrupted turn recovery](message-routing.md#interrupted-turn-recovery)
ledger tracks running work separately so restarts resume unfinished turns without waking idle
conversations.

---

**Want to customize this?** Write your own memory backend plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
