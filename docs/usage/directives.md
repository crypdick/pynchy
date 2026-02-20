# Directives

This page explains how to configure system prompt directives — the mechanism that controls what instructions your agents receive. Understanding directives helps you customize agent behavior per-workspace without editing code.

## What Directives Are

Directives are markdown files that get injected into an agent's system prompt. They contain behavioral instructions, persona definitions, tool usage guidance, and anything else that shapes how the agent operates. Each directive is scoped to specific workspaces, so you can give admin agents different instructions than regular group agents.

## How They Work

Directives are configured in `config.toml` under `[directives.*]` sections. Each directive has:

- **`file`** — Path to a markdown file (relative to project root)
- **`scope`** — Which workspaces receive this directive

When a container launches, the host resolves which directives match the workspace, reads the files, concatenates them in sorted key order, and passes the result into the agent's system prompt.

## Configuring Directives

```toml
[directives.base]
file = "directives/base.md"
scope = "all"

[directives.idle-escape]
file = "directives/idle-escape.md"
scope = "all"

[directives.admin-ops]
file = "directives/admin-ops.md"
scope = ["admin-1", "admin-2"]

[directives.pynchy-dev]
file = "directives/pynchy-dev.md"
scope = "crypdick/pynchy"
```

## Scope Rules

The `scope` field determines which workspaces receive the directive:

| Scope Value | Matches |
|-------------|---------|
| `"all"` | Every workspace |
| `"folder-name"` | Workspace with that folder name |
| `"owner/repo"` | Workspaces whose `repo_access` equals the slug |
| `["a", "b"]` | Union — matches any of the listed scopes |

Omitting `scope` (or setting it to `null`) means the directive never matches — a warning is logged.

## File Location and Format

Directive files live under `directives/` in the project root. They're plain markdown — write them the same way you'd write a CLAUDE.md file. Multiple matching directives are concatenated with `---` separators, sorted by their config key name.

Files ending in `.EXAMPLE` are automatically ignored (they exist as templates in the repo).

## Relationship to CLAUDE.md

Directives are additive to the project's `CLAUDE.md`. Admin and repo_access workspaces have their `cwd` set to `/workspace/project`, so Claude Code discovers the project-root `CLAUDE.md` natively. Directives provide additional instructions on top of that — things like persona, communication style, and operational procedures that aren't appropriate for the project CLAUDE.md.

## KV Cache Considerations

Directive content is stable across session resumes — it doesn't change between runs. This means the system prompt stays constant, preserving the API's KV cache. Avoid putting ephemeral or frequently-changing content in directives; use system notices for per-run context instead.
