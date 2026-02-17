# Memory

This page explains how agents remember things across conversations. Understanding memory helps you work effectively with your assistant — it can save preferences, recall past decisions, and maintain context between sessions.

Pynchy's memory subsystem is pluggable. The built-in plugin uses SQLite with full-text search, but alternative backends can be swapped in via plugins.

## How Memory Works

Every group has **isolated memory** — what one group saves, another cannot access. Agents have four tools for managing persistent memory:

| Tool | What it does |
|------|-------------|
| `save_memory` | Store a fact with a key and content |
| `recall_memories` | Search memories by keyword |
| `forget_memory` | Remove a memory by key |
| `list_memories` | List all saved memory keys |

Each memory has a **key** (unique identifier), **content**, and a **category**:

| Category | Purpose |
|----------|---------|
| `core` | Permanent facts (default) — your preferences, project details, recurring instructions |
| `daily` | Session context — ephemeral notes for the current work |
| `conversation` | Auto-archived conversation summaries (created automatically when sessions compact) |

You don't need to manage categories yourself — the agent picks the right one based on context. `core` is the default for most things you ask it to remember.

## File-Based Memory

In addition to the structured memory tools above, agents have file-based storage:

- **Per-group memory** — Each group has a folder under `groups/{name}/` with its own `CLAUDE.md`. The agent reads this on every run.
- **Global memory** — `groups/global/CLAUDE.md` is shared read-only with all non-god groups. Only the god channel can write to it.
- **Project-access groups** — Groups with `project_access` get a worktree mount instead of the global mount, and read `CLAUDE.md` from the project tree.
- **Files** — Groups can create and read files in their folder and reference them in conversations.

### Conversation Archives

When a session compacts (context gets too long), the agent automatically archives the conversation to both:

- A markdown file in the group's `conversations/` folder
- Structured memory with category `conversation` (searchable via `recall_memories`)

## Built-in: sqlite-memory

The default memory backend uses **SQLite with FTS5 full-text search**.

### How Search Works

`recall_memories` uses a two-tier search strategy:

1. **BM25 full-text search** — SQLite FTS5 tokenizes content and ranks results by term frequency. Best for natural language queries ("favorite color", "project deadline").
2. **LIKE fallback** — If FTS5 returns no results, falls back to substring matching. Catches queries that don't tokenize well (URLs, special characters, partial words).

### Storage Details

Memories live in `data/memories.db` — a dedicated SQLite database separate from the main `messages.db`. The memory plugin manages its own connection with WAL mode for concurrent access.

---

**Want to customize this?** Write your own memory backend plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
