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

Scheduled tasks and workspace state live in the database, but the **source of truth is `config.toml`** (and plugin specs). On every startup, `reconcile_workspaces()` syncs the declared configuration into the database:

1. Merges plugin specs with `config.toml` workspaces
2. Creates chat groups for workspaces missing database entries
3. Creates or updates scheduled tasks for periodic agents (those with `schedule` + `prompt`)
4. Seeds `CLAUDE.md` from plugin templates if the file doesn't exist
5. Creates channel aliases across messaging platforms

### Automatic config-to-database sync

For periodic agents, the reconciler compares the database row against `config.toml` on every startup. If any of the following fields differ, it patches the database to match:

- **`schedule`** — also recalculates `next_run` when the cron expression changes
- **`prompt`** — updates the prompt sent to the agent on each scheduled run
- **`repo_access`** — updates the repo worktree mount

This means editing `config.toml` and restarting the service is all that's needed to change a schedule, prompt, or repo access. No manual database edits required.

## Workspace Config Fields

| Field | Type | Description |
|-------|------|-------------|
| `is_admin` | `bool` | Whether this is an admin workspace |
| `repo_access` | `str` | GitHub slug (`owner/repo`) from `[repos.*]`; mounts a project worktree |
| `schedule` | `str` | Cron expression for periodic execution |
| `prompt` | `str` | Prompt sent to the agent on each scheduled run |
| `context_mode` | `str` | `"group"` (shared session) or `"isolated"` (fresh each time) |
| `requires_trigger` | `bool` | Whether messages need the @mention prefix |
| `name` | `str` | Display name (defaults to folder titlecased) |
| `security` | `dict` | MCP tool access control and rate limiting |

---

**Want to customize this?** Write your own workspace plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
