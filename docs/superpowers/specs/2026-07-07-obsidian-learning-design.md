# Obsidian Learning Design

## Context

Pynchy already has two relevant extension points:

- host-side service tools that agents call through the container MCP server and IPC;
- session preparation that copies selected skill directories into the agent session before container start.

The first learning phase uses those surfaces instead of mounting the Obsidian vault directly into containers. The vault becomes the durable source of truth for learned facts and learned skills, while Pynchy remains responsible for deciding what agents can search, write, validate, and activate.

## Scope

This phase adds:

- one global Obsidian-backed memory namespace;
- one Obsidian-backed learned-skill namespace;
- a durable background learning queue that reviews completed turns without blocking chat replies;
- a constrained host-side MCP/service-tool facade for vault search, memory writes, and skill writes.

This phase does not add per-group, per-policy, or per-user access control. All sessions share the same global learning surface. The design leaves namespace metadata in place so access control can attach later without rewriting stored notes.

## Approaches Considered

### Recommended: Obsidian-backed learning with Pynchy activation

Pynchy searches the configured vault root for recall, writes new learning notes into a controlled global memory folder, stores learned skills under a controlled skill namespace, and validates skills before adding them to the active skill registry.

This keeps recall broad, writes auditable, and behavior-changing instructions behind Pynchy validation.

### Rejected: Raw vault mount inside agent containers

Mounting the vault into every agent container would make implementation simple, but it would also give agents direct filesystem access to unrelated notes, let them write anywhere, and bypass Pynchy's IPC/security model.

### Deferred: Full memory namespace access control

Pynchy can later resolve namespace attachments from workspace/profile/group policy. That adds useful isolation but is not needed for the first shared-memory implementation.

## Namespace Model

The first version has two configured namespaces.

```toml
[learning]
enabled = true
review_after_turn = true

[learning.obsidian]
vault_root = "~/Documents/obsidian/wiki"
global_search_root = "."
global_write_root = "repos/<owner>/<repo>/memory/global"
skill_root = "repos/<owner>/<repo>/skills"
```

`global_search_root = "."` means the global memory namespace can recall from the whole vault. Automatic writes do not target the vault root. They target `global_write_root` so newly learned notes stay easy to audit, migrate, and clean up.

Every generated memory note includes frontmatter:

```yaml
---
learning_namespace: global
learning_kind: fact
source: pynchy-learning-review
visibility: shared
---
```

Every generated skill lives under:

```text
<skill_root>/<skill-name>/SKILL.md
```

The folder name is the skill identifier. Optional companion files can live below the same skill folder, but the first implementation validates and activates only skills with a `SKILL.md` entrypoint.

## Constrained Vault Tools

Agents and background reviewers do not receive shell access to the vault. They get host-side service tools exposed through the existing container MCP/IPC path:

| Tool | Purpose | Constraint |
|------|---------|------------|
| `learning_search` | Search the global vault namespace | Searches only `global_search_root` and returns bounded snippets |
| `learning_read` | Read a selected search result | Reads only opaque result ids returned by `learning_search` |
| `learning_write_memory` | Save a learned fact or preference | Writes only under `global_write_root` |
| `learning_list_skills` | List learned skills | Reads only under `skill_root` |
| `learning_write_skill` | Create or update a learned skill | Writes only under one skill folder below `skill_root` |

The host resolves and normalizes all paths. Requests with path traversal, absolute paths, symlinks escaping the configured roots, or oversized payloads fail before touching the vault.

## Learning Queue

Foreground message handling stays fast:

1. Pynchy processes a user turn normally.
2. After the final assistant result is persisted, Pynchy writes a bounded learning packet to a durable queue.
3. A host-side learning worker claims packets with lease/retry semantics.
4. The worker coalesces recent packets from the same chat during a short idle window.
5. A deterministic prefilter skips obvious casual turns.
6. The reviewer model receives only the bounded packet and the constrained vault tools.
7. Accepted outputs write immediately to Obsidian memory or the learned-skill namespace.

The queue uses file-based IPC semantics because Pynchy already depends on atomic filesystem handoff between containers and the host. Durable work units live outside the existing synchronous `tasks/` request-response directory:

```text
data/ipc/learning/
├── pending/
├── claimed/
├── done/
└── errors/
```

Claim files include lease expiry and attempt count. On startup, expired claimed files move back to `pending/` unless they exceeded the retry limit.

## Learning Packet

The packet must be small enough that per-turn review cost stays roughly linear instead of growing with transcript length.

It contains:

- workspace/group identifiers;
- new user messages for the completed turn;
- final assistant answer;
- tool names and counts;
- loaded skill names;
- short error/recovery snippets when a task failed and then recovered;
- message ids for provenance.

It does not include the full conversation transcript. The reviewer can search the vault when it needs prior facts.

## Skill Activation

Learned skills are stored in Obsidian, but Pynchy activates only validated skills.

Validation requires:

- a skill folder below `skill_root`;
- a `SKILL.md` file with valid frontmatter;
- a non-empty name and description;
- file sizes below configured limits;
- no symlink escapes from the skill folder.

After validation, session preparation treats learned skills as an additional skill source alongside built-in and plugin skill paths. Existing workspace skill selection still applies. In the first phase, learned skills use a single namespace/tier name such as `learned`.

## Error Handling

Vault misconfiguration disables learning writes but does not block normal chat processing. Pynchy logs an operator-visible error when:

- `vault_root` does not exist;
- a configured root escapes `vault_root`;
- the Obsidian search/index command fails;
- the worker exhausts retry attempts for a learning packet;
- a generated skill fails validation.

Failed queue items move to `data/ipc/learning/errors/` with the original packet, attempt count, and error summary.

## Testing

Unit tests cover:

- config parsing and path normalization for the Obsidian learning settings;
- rejection of path traversal, absolute escaped paths, and symlink escapes;
- memory writes creating frontmatter under `global_write_root`;
- whole-vault search using `global_search_root = "."` without granting arbitrary reads;
- skill validation and active skill source discovery;
- durable queue claim, lease expiry, retry, done, and error behavior;
- post-turn enqueue behavior after a successful agent result;
- prefilter behavior for obvious no-op turns.

Integration tests use temporary vault directories and fake search results. The default test suite must not require a real Obsidian vault, Ollama, or the external `obsidian-knowledge` CLI.

## Follow-Up Work

Future namespace access control can add:

- workspace/profile-attached memory namespaces;
- separate read and write namespace grants;
- per-group or per-policy skill namespaces;
- reviewer modes such as immediate, cop-reviewed, or read-only;
- migration tools that move existing `global` notes into narrower namespaces.
