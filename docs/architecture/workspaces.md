# Workspaces

How managed workspace definitions work under the hood. Use this page to build plugins that ship preconfigured agents — periodic code reviewers, monitoring bots, or anything that should "just work" after installation.

Workspaces are configured chat roots. A workspace binds one Discord/Slack channel to a profile. Dynamic conversations under that root, such as Discord threads, get their own isolated runtime folders and inherit the workspace profile.

## What Workspace Specs Do

A workspace spec declares: "this channel should exist and should use this profile." The profile carries policy and capabilities such as admin status, repo access, model routing, MCP servers, skills, and whether the workspace filesystem contains secrets.

At startup, Pynchy **reconciles** workspace specs against the database, creating configured chat roots when the channel plugin supports provisioning. Config-backed jobs under `[jobs.*]` create scheduled agent tasks or host cron jobs. Agent instructions are delivered via [prompts](../usage/prompts.md) rather than seeded files.

## Config Merging

Workspace specs come from two sources: plugins and `config.toml`. When both define the same workspace folder, **user config always wins**.

```
Plugin provides:   workspace + profile config
User overrides:    [workspaces.same-folder] in config.toml
Result:            User config takes priority
```

This lets plugins provide sensible defaults while users retain full control.

## Reconciliation

Scheduled tasks and workspace state live in the database, but the **source of truth is `config.toml`** (and plugin specs). On every startup, `reconcile_workspaces()` syncs the declared config into the database:

1. Merges plugin specs with `config.toml` workspaces
2. Creates chat groups for workspaces missing database entries
3. Creates or updates scheduled tasks from `[jobs.*]` agent jobs
4. Creates channel aliases across messaging platforms

### Automatic config-to-database sync

For config-backed jobs, the reconciler compares the database row against `config.toml` on every startup. If any of these fields differ, it patches the database to match:

- **`schedule`** — also recalculates `next_run` when the cron expression changes
- **`prompt`** — updates the prompt sent to the agent on each scheduled run
- **delivery chat** — follows the target workspace's current registered JID
- **`repo_access`** — updates the repo worktree mount from the target profile
- **`context_mode`** — updates the scheduled run context mode

To change a schedule, prompt, repo access, or model override, edit `config.toml` and restart the service. No manual database edits required.

## Workspace Config Fields

| Field | Type | Description |
|-------|------|-------------|
| `profile` | `str` | Name of a profile from `[profiles.*]` |
| `chat` | `str` | Connection chat ref, such as `connection.discord.main.chat.synapse.channels.admin` |
| `name` | `str` | Display name (defaults to folder titlecased) |

## Profile Config Fields

| Field | Type | Description |
|-------|------|-------------|
| `tags` | `list[str]` | Capability tags used by prompt, MCP, and skill selection |
| `is_admin` | `bool` | Whether workspaces using this profile get admin privileges |
| `contains_secrets` | `bool` | Whether workspace files may contain secrets |
| `repo_access` | `str` | GitHub slug (`owner/repo`) from `[repos.*]`; mounts a project worktree |
| `model` | `str` | Optional model override |
| `fallback_model` | `str` | Optional fallback model override |
| `context_mode` | `str` | `"group"` (shared session) or `"isolated"` (fresh each scheduled run) |
| `trigger` | `str` | `"mention"` or `"always"` — whether @mention is required |
| `skills` | `list[str]` | Skill tier names and/or skill names to include; `"*"` = include all |
| `mcp_servers` | `list[str]` | MCP server names and group names to attach |
| `security` | `dict` | Per-workspace service trust overrides |

---

**Want to customize this?** Write your own workspace plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
