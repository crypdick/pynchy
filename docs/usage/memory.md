# Memory

How agents remember things across conversations. The memory subsystem lets agents save preferences, recall past decisions, and keep context between sessions.

The memory subsystem is pluggable. The built-in plugin uses SQLite with full-text search, but you can swap in alternative backends via plugins.

## How Memory Works

Every group has **isolated memory** — one group can't read another's. Agents have four tools for persistent memory:

| Tool | What it does |
|------|-------------|
| `save_memory` | Store a fact with a key and content |
| `recall_memories` | Search memories by keyword |
| `forget_memory` | Remove a memory by key |
| `list_memories` | List all saved memory keys |

Each memory has a **key** (unique identifier), **content**, and a **category**:

| Category | Purpose |
|----------|---------|
| `core` | Permanent facts (default) — preferences, project details, recurring instructions |
| `daily` | Session context — ephemeral notes for the current work |
| `conversation` | Auto-archived conversation summaries (created when sessions compact) |

You don't manage categories yourself — the agent picks the right one based on context. `core` is the default for most things you ask it to remember.

## File-Based Memory

On top of the structured tools above, agents have file-based storage:

- **Per-group memory** — Each group has a folder under `groups/{name}/`. The agent reads files there on every run.
- **Files** — Groups can create and read files in their folder and reference them in conversations.

## Obsidian Automatic Learning

Automatic learning can use an Obsidian vault as a shared memory and skill namespace. Enable `[learning]` and set `[learning.obsidian].vault_root` to the vault root. Pynchy mounts that root read-write at `/workspace/vault` by default.

After each successful turn, Pynchy starts a Temporal learning review workflow. The workflow runs a hidden reviewer agent that decides whether the turn produced durable memory or skill updates. Set `max_attempts` to control Temporal activity retries for that reviewer.

The vault root is the global memory namespace. The hidden reviewer chooses existing semantic folders first, then falls back to `systems/pynchy/profiles/{profile}/memory` when no repo, machine, subject, or other existing folder clearly fits.

Learned skills live under `systems/pynchy/profiles/{profile}/skills/<skill-name>/SKILL.md`. To load them into a profile or workspace, include `skills = ["learned"]` or `skills = ["*"]` in the effective workspace config.

### Conversation Archives

When a session compacts (context gets too long), the agent archives the conversation to both:

- A markdown file in the group's `conversations/` folder
- Structured memory with category `conversation` (searchable via `recall_memories`)

## Built-in: sqlite-memory

The default backend uses **SQLite with FTS5 full-text search**.

### How Search Works

`recall_memories` uses a two-tier strategy:

1. **BM25 full-text search** — SQLite FTS5 tokenizes content and ranks results by term frequency. Best for natural language queries ("favorite color", "project deadline").
2. **LIKE fallback** — If FTS5 returns nothing, fall back to substring matching. Catches queries that don't tokenize well (URLs, special characters, partial words).

### Storage Details

Memories live in `data/memories.db` — a dedicated SQLite database separate from the main `messages.db`. The memory plugin manages its own connection with WAL mode for concurrent access.

---

**Want to customize this?** Write your own memory backend plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
