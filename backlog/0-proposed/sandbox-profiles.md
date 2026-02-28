# Sandbox Profiles and Universal Config

## Problem

Directive assignment uses a separate top-level `[directives.*]` config with scope matching. This is inconsistent with how `skills` and `mcp_servers` are assigned (per-sandbox fields). Understanding what a workspace gets requires mentally evaluating scope rules across multiple config sections.

## Proposed Design

Replace `[directives.*]` sections with a three-tier union model:

```
effective_config = sandbox_universal ∪ sandbox_profile ∪ per-sandbox
```

### sandbox_universal

Applies to every sandbox automatically. Replaces `scope = "all"` directives.

```toml
[sandbox_universal]
directives = ["base", "idle-escape"]
```

### sandbox_profiles

Reusable bundles of directives, skills, and other config. Referenced by name from sandboxes.

```toml
[sandbox_profiles.pynchy-dev]
directives = ["admin-ops", "pynchy-code-improver"]
skills = ["core", "ops"]

[sandbox_profiles.external-repo]
# nothing extra — just gets universal
```

### Per-sandbox

Sandboxes reference a profile and can add their own overrides. Effective config is the union of all three tiers.

```toml
[sandbox.admin-1]
profile = "pynchy-dev"
chat = "connection.slack.synapse.chat.admin-1"
is_admin = true
```

Effective admin-1: directives = ["base", "idle-escape", "admin-ops", "pynchy-code-improver"], skills = ["core", "ops"].

## What This Eliminates

- `[directives.*]` top-level sections (directive files stay in `directives/`, assignment moves to profiles)
- Hidden scope resolution logic in `directives.py`
- The `scope` field entirely

## What This Preserves

- Directive markdown files in `directives/`
- Skill tier filtering (skills field still uses tier names and skill names)
- Per-sandbox MCP server lists
