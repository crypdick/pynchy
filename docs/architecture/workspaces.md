# Workspaces

This page explains how managed workspace definitions work under the hood. Understanding this helps you build plugins that ship preconfigured agents — periodic code reviewers, monitoring bots, or any agent that should "just work" after installation.

Workspaces are pluggable. Plugins can provide workspace specs that create groups, scheduled tasks, and seed `CLAUDE.md` files automatically.

## What Workspace Specs Do

A workspace spec is a declaration: "this agent should exist with these settings." It includes a folder name, configuration (schedule, prompt, access level), and optionally seed content for the group's `CLAUDE.md`.

At startup, Pynchy **reconciles** workspace specs against the database — creating groups, scheduling tasks, and seeding files as needed. This means a plugin can ship a fully configured periodic agent that activates the moment you install the package.

## Config Merging

Workspace specs come from two sources: plugins and `config.toml`. When both define the same workspace folder, **user config always wins** for settings, but the plugin's `claude_md` template is preserved for first-run seeding.

```
Plugin provides:   folder + config + claude_md
User overrides:    [workspaces.same-folder] in config.toml
Result:            User config takes priority, plugin claude_md seeds on first run
```

This lets plugins provide sensible defaults while users retain full control.

## Reconciliation

On startup, `reconcile_workspaces()`:

1. Merges plugin specs with `config.toml` workspaces
2. Creates chat groups for workspaces missing database entries
3. Creates or updates scheduled tasks for periodic agents (those with `schedule` + `prompt`)
4. Seeds `CLAUDE.md` from plugin templates if the file doesn't exist
5. Creates channel aliases across messaging platforms

## Workspace Config Fields

| Field | Type | Description |
|-------|------|-------------|
| `is_god` | `bool` | Whether this is an admin workspace |
| `project_access` | `bool` | Mount a project worktree instead of global memory |
| `schedule` | `str` | Cron expression for periodic execution |
| `prompt` | `str` | Prompt sent to the agent on each scheduled run |
| `context_mode` | `str` | `"group"` (shared session) or `"isolated"` (fresh each time) |
| `requires_trigger` | `bool` | Whether messages need the @mention prefix |
| `name` | `str` | Display name (defaults to folder titlecased) |
| `security` | `dict` | MCP tool access control and rate limiting |

---

**Want to customize this?** Write your own workspace plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
