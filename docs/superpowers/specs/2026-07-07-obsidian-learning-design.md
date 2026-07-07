# Obsidian Learning Design

## Context

Pynchy already has two relevant extension points:

- host-side service tools that agents call through the container MCP server and IPC;
- session preparation that copies selected skill directories into the agent session before container start.

The first learning phase keeps the integration intentionally direct: Pynchy mounts the configured Obsidian vault into learning-enabled containers. The vault becomes the durable source of truth for learned facts and learned skills, and folder placement carries the meaning.

The current vault organizes knowledge by path. Examples include `repos/<owner>/<repo>/memory/` for project memory, `systems/machines/<host>/memory/` for machine memory, `diary/` for dated operational notes, `convos/` for conversation writeups, `plans/` and `specs/` for project artifacts, and `_sources/` for source material. Pynchy should follow that convention instead of relying on agents to maintain semantic frontmatter correctly.

## Scope

This phase adds:

- one global Obsidian-backed memory namespace;
- one profile-scoped fallback location for learned memory and skills;
- a durable background learning queue that reviews completed turns without blocking chat replies;
- a direct vault mount that lets agents and background reviewers read and write vault files through normal filesystem tools.

This phase does not add per-group, per-policy, or per-user access control. All sessions can read the same mounted vault, while fallback writes use the workspace's sandbox profile so worker pools can share learned facts and skills. The folder layout stays stable so access control can attach later by narrowing the mounted root or selecting specific subfolders.

## Approaches Considered

### Recommended: Mounted Obsidian vault with folder-based activation

Pynchy mounts the configured vault root into learning-enabled containers, writes new learning notes into conventional vault folders, stores learned skills under a conventional skill folder, and validates skills before adding them to the active skill registry.

This keeps the first implementation small and matches the way the vault already works. Behavior-changing instructions still go through Pynchy validation before activation.

### Deferred: Constrained vault MCP facade

A future version can replace the broad filesystem mount with a constrained MCP/service-tool facade. That facade can expose search, read, and write operations for only the namespaces attached to a session.

### Deferred: Full memory namespace access control

Pynchy can later resolve namespace attachments from workspace/profile/group policy. That adds useful isolation but is not needed for the first shared-memory implementation.

## Namespace Model

The first version has one mounted global vault namespace and one configured profile fallback path.

```toml
[learning]
enabled = true
review_after_turn = true

[learning.obsidian]
vault_root = "~/Documents/obsidian/wiki"
mount_path = "/workspace/vault"
default_profile_root = "systems/pynchy/profiles/{profile}"
```

`vault_root` is the global memory namespace. In the first implementation, Pynchy mounts that root directly into each learning-enabled container. A later access-control phase can change `vault_root` to a subdirectory or mount a narrower `vault_mount_root` without changing the folder conventions for memory and skills.

Automatic memory writes follow the current vault pattern by choosing the relevant folder. Repo-associated work can write under `repos/<owner>/<repo>/memory/`; machine work can write under `systems/machines/<host>/memory/`; non-coding work can write under its semantic area of the vault. When no better domain folder is obvious, the reviewer uses the configured profile fallback:

```text
<vault_root>/systems/pynchy/profiles/<profile>/memory/
├── MEMORY.md
├── index.md
└── <date-or-descriptive-slug>.md
```

Pynchy resolves `<profile>` from `workspace.profile`. Workspaces without an explicit profile use `default`. Multiple workspaces with the same profile therefore share fallback memory and learned skills.

Pynchy does not require learning-specific frontmatter. Agents classify information by choosing the correct folder and filename. If the vault linter manages generic `created` or `updated` fields, that remains a vault concern rather than a learning contract.

Learned skills live under:

```text
<vault_root>/systems/pynchy/profiles/<profile>/skills/<skill-name>/SKILL.md
```

The folder name is the skill identifier. Optional companion files can live below the same skill folder, but the first implementation validates and activates only skills with a `SKILL.md` entrypoint.

## Vault Mount

Learning-enabled containers receive the vault at `mount_path`.

```text
host:      <vault_root>/
container: /workspace/vault/
mode:      read-write
```

The broad mount is a deliberate v1 simplification. It gives agents direct access to the vault, so operators should enable it only for sessions trusted to read and edit that vault. The follow-up access-control design can narrow the mount to a subdirectory or replace it with a constrained MCP facade.

Pynchy still validates mount configuration before container start:

- `vault_root` must exist;
- `vault_root` must resolve to a directory;
- `mount_path` must be an absolute container path;
- `default_profile_root` must be a relative path under `vault_root`;
- `default_profile_root` may contain `{profile}`, which Pynchy expands from the sanitized sandbox profile name.

## Learning Queue

Foreground message handling stays fast:

1. Pynchy processes a user turn normally.
2. After the final assistant result is persisted, Pynchy writes a bounded learning packet to a durable queue.
3. A host-side learning worker claims packets with lease/retry semantics.
4. The worker coalesces recent packets from the same chat during a short idle window.
5. A deterministic prefilter skips obvious casual turns.
6. The reviewer model receives only the bounded packet and the mounted vault path.
7. Accepted outputs write immediately to the selected vault memory folder or the resolved profile's learned-skill namespace.

The queue uses file-based IPC semantics because Pynchy already depends on atomic filesystem handoff between containers and the host. Durable work units live outside the existing synchronous `requests/` request-response directory:

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

It does not include the full conversation transcript. The reviewer can search the mounted vault when it needs prior facts.

## Skill Activation

Learned skills are stored in Obsidian, but Pynchy activates only validated skills.

Validation requires:

- a skill folder below the resolved profile's `skills/` folder;
- a `SKILL.md` file with the metadata required by Pynchy's skill loader;
- a non-empty name and description;
- file sizes below configured limits;
- no symlink escapes from the skill folder.

After validation, session preparation treats learned skills from the resolved profile as an additional skill source alongside built-in and plugin skill paths. Existing workspace skill selection still applies. In the first phase, learned skills use a single namespace/tier name such as `learned`.

## Error Handling

Vault misconfiguration disables the vault mount and learning writes but does not block normal chat processing unless the workspace explicitly requires learning. Pynchy logs an operator-visible error when:

- `vault_root` does not exist;
- `vault_root` is not a directory;
- `default_profile_root` is absolute or escapes `vault_root`;
- the container runtime rejects the vault mount;
- the worker exhausts retry attempts for a learning packet;
- a generated skill fails validation.

Failed queue items move to `data/ipc/learning/errors/` with the original packet, attempt count, and error summary.

## Testing

Unit tests cover:

- config parsing and path normalization for the Obsidian learning settings;
- rejection of path traversal, absolute escaped paths, and symlink escapes;
- memory writes creating notes under the configured profile fallback when no semantic folder is selected;
- vault mount generation for learning-enabled containers;
- skill validation and active skill source discovery for workspaces sharing a profile;
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
- mounting only a subdirectory for less-trusted sessions;
- a constrained MCP facade for sessions that should search or write only selected folders.
