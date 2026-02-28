# Sandbox Profiles Design

**Date**: 2026-02-28
**Status**: Approved

## Problem

Directive assignment uses separate top-level `[directives.*]` config sections with scope matching. This is inconsistent with how `skills` and `mcp_servers` are assigned (per-sandbox fields). Understanding what a sandbox gets requires mentally evaluating scope rules across multiple config sections. The `[workspace_defaults]` section adds a fourth concept. The result: four different mechanisms for "what does this sandbox get?"

## Solution

Replace `[directives.*]` and `[workspace_defaults]` with a three-tier union model:

```
effective_config = sandbox_universal ∪ sandbox_profile ∪ per-sandbox
```

After the config-level merge, the existing connection/chat security cascade applies on top:

```
sandbox_universal < profile < per-sandbox < connection.security < chat.security
```

## Data Model

### `SandboxProfileConfig` (new Pydantic model)

All fields `Optional` with `None` default, meaning "not set at this tier, inherit from next."

**Union fields** (merged across tiers, deduplicated, order-preserved):
- `directives: list[str] | None` — directive names → convention-resolved to `directives/<name>.md`
- `skills: list[str] | None` — tier names and/or skill names; `"*"` = include all
- `mcp_servers: list[str] | None` — server names + group names

**Override fields** (most-specific explicitly-set value wins):
- `context_mode`, `access`, `mode`, `trust`, `trigger`, `idle_terminate`, `git_policy`
- `allowed_users: list[str] | None` — override semantics (not union)
- `security: WorkspaceSecurityTomlConfig | None`
- `repo_access: str | None`

### Settings changes

- **Remove**: `workspace_defaults: WorkspaceDefaultsConfig`, `directives: dict[str, DirectiveConfig]`
- **Add**: `sandbox_universal: SandboxProfileConfig`, `sandbox_profiles: dict[str, SandboxProfileConfig]`
- `WorkspaceConfig` gains: `profile: str | None`, `directives: list[str] | None`
- Validator: `profile` references must exist in `sandbox_profiles`

### Merge semantics

Explicit-vs-default tracking uses both `model_fields_set` and `None` sentinel:
- Field in `model_fields_set` with a value → explicitly set, use it
- Field in `model_fields_set` with `None` → explicitly cleared/reset
- Field not in `model_fields_set` → not set at this tier, inherit from next

**Union fields**: `deduplicate(universal_list + profile_list + sandbox_list)`
**Override fields**: first explicitly-set value from most-specific tier wins

### `ResolvedSandboxConfig` (new frozen dataclass)

Holds fully-resolved values. Downstream code consumes this instead of querying `WorkspaceConfig` + `resolve_directives()` separately. Contains all merged list fields, resolved scalars, and pass-through fields from `WorkspaceConfig` (`chat`, `is_admin`, `schedule`, `prompt`, `name`).

## Directive Resolution

Convention-based: directive name `"base"` → file `directives/base.md`. No config registry needed.

New function `read_directives(names: list[str], project_root: Path) -> str | None` reads files and concatenates. No scope logic.

## Wildcard Standardization

Replace `"all"` with `"*"` everywhere: skills lists, `allowed_users`, any "match everything" sentinel. Aligns with glob convention.

## Logging

Rich merge logging in `merge_sandbox_config()`:
- `debug` level for every field resolution
- `info` level when a value at one tier overrides another tier's value
- Logs source tier and overridden values for each field

## Config Migration

### Before

```toml
[workspace_defaults]
context_mode = "group"
access = "readwrite"
mode = "agent"
trust = true
trigger = "mention"
allowed_users = ["*"]

[directives.base]
file = "directives/base.md"
scope = "all"

[directives.idle-escape]
file = "directives/idle-escape.md"
scope = "all"

[directives.admin-ops]
file = "directives/admin-ops.md"
scope = ["admin-1", "admin-2"]

[directives.pynchy-code-improver]
file = "directives/pynchy-code-improver.md"
scope = "crypdick/pynchy"

[directives.ray-bench]
file = "directives/ray-bench.md"
scope = "crypdick/ray-bench-workspace"

[sandbox.admin-1]
chat = "connection.slack.synapse.chat.admin-1"
is_admin = true
idle_terminate = false
trigger = "always"
repo_access = "crypdick/pynchy"
skills = ["core", "ops"]
```

### After

```toml
[sandbox_universal]
directives = ["base", "idle-escape"]
context_mode = "group"
access = "readwrite"
mode = "agent"
trust = true
trigger = "mention"
allowed_users = ["*"]

[sandbox_profiles.pynchy-dev]
directives = ["pynchy-admin-ops", "pynchy-code-improver"]
skills = ["core", "ops"]
repo_access = "crypdick/pynchy"
idle_terminate = false
trigger = "always"

[sandbox_profiles.ray-bench]
directives = ["ray-bench"]
repo_access = "crypdick/ray-bench-workspace"
mcp_servers = ["notebook"]

[sandbox.admin-1]
profile = "pynchy-dev"
chat = "connection.slack.synapse.chat.admin-1"
is_admin = true

[sandbox.code-improver]
profile = "pynchy-dev"
chat = "connection.slack.synapse.chat.pynchy-core-code-improver"
context_mode = "isolated"
```

Sandboxes without a profile (gantt, personal-tasks, anyscale) get `sandbox_universal` only.

## File Changes

### Modify

| File | Change |
|------|--------|
| `src/pynchy/config/models.py` | Add `SandboxProfileConfig`. Add `profile`, `directives` to `WorkspaceConfig`. Remove `DirectiveConfig`, `WorkspaceDefaultsConfig`. |
| `src/pynchy/config/settings.py` | Replace `workspace_defaults` with `sandbox_universal`. Add `sandbox_profiles`. Remove `directives`. Add profile-ref validator. |
| `src/pynchy/config/directives.py` | Rewrite: delete scope logic, new `read_directives(names, project_root)`. |
| `src/pynchy/config/access.py` | Update cascade base tier from `workspace_defaults` to `sandbox_universal`. |
| `src/pynchy/host/orchestrator/workspace_config.py` | `load_workspace_config()` calls merge, returns resolved config. |
| `src/pynchy/host/orchestrator/agent_runner.py` | Remove separate `resolve_directives()` call. |
| `src/pynchy/host/container_manager/session_prep.py` | `_is_skill_selected`: `"all"` → `"*"`. |
| `tests/test_directives.py` | Rewrite for new API. |
| `config-examples/config.toml.EXAMPLE` | Reflect new structure. |

### Add

| File | Purpose |
|------|---------|
| `src/pynchy/config/merge.py` | `merge_sandbox_config()`, `ResolvedSandboxConfig`, merge logging. |
| `tests/test_merge.py` | Tests for union semantics, override semantics, logging, cascade. |

### Rename

| From | To |
|------|-----|
| `directives/admin-ops.md` | `directives/pynchy-admin-ops.md` |

### Delete

| Item | Reason |
|------|--------|
| `DirectiveConfig` model | Convention-based resolution replaces it. |
| `WorkspaceDefaultsConfig` model | Absorbed into `SandboxProfileConfig`. |
| `_scope_matches()` | No more scope evaluation. |

### Production migration

Update `config.toml` on pynchy-server per the after example above.

## Documentation

Update architecture and configuration docs using the docs skill as the final commit before deploying. Key pages to update:
- Architecture page (config model, directive resolution)
- Any pages referencing `[directives.*]` config or `[workspace_defaults]`
- Plugin authoring docs if they reference directive scoping

## Verification

- `uv run pytest tests/` passes
- `uv run mkdocs build --strict` passes
- Config example template reflects new model
- Production config.toml on pynchy-server migrated and service restarted
