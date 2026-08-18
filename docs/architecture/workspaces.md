# Workspaces

How managed workspace definitions work under the hood. Use this page to build plugins that ship preconfigured agents — periodic code reviewers, monitoring bots, or anything that should "just work" after installation.

Workspaces are policy owners. A root workspace binds a channel chat to profiles.
A semantic workspace scope binds its own profiles to a physical root without
requiring another Discord channel. Dynamic conversations get isolated runtime
folders and inherit their policy owner, which may differ from the Discord
category that physically contains them.

## What Workspace Specs Do

A workspace spec declares which profiles a workspace selects. Profiles carry reusable defaults for admin status, repo mounts, model routing, selected tools, skills, and whether the workspace filesystem contains secrets. A workspace can override its model directly.

At startup, Pynchy **reconciles** workspace specs against the database, creating configured chat roots when the channel plugin supports provisioning. File-backed automations create scheduled agent tasks or host cron jobs. Agent instructions are delivered via [prompts](../usage/prompts.md) rather than seeded files.

Workspace specs can also declare named child threads. The reconciler creates
or reuses each thread below its configured root, then registers a dynamic
workspace. A thread without `workspace` inherits the root's complete profile.
A semantic thread names `workspace` and `profiles`
to own distinct policy. It requires a channel to support
thread lookup as well as creation; creation without lookup would make startup
non-idempotent. The thread reconciler supports a dry-run mode that returns its
planned actions without changing a channel or runtime registration.

Use `scopes` when many logical owners share one visible category:

```toml
# data/personalization/workspaces/relationships.toml
schema_version = 1

[workspace]
profiles = ["relationships"]

[[workspace.scopes]]
workspace = "fam"
profiles = ["fam"]
```

```toml
# data/personalization/workspaces/admin.toml
schema_version = 1

[workspace]
profiles = ["admin"]

[[workspace.scopes]]
workspace = "pynchy-dev"
profiles = ["pynchy-dev"]
```

Here, Fam work is physically placed below `relationships` but uses only the
`fam` policy. Pynchy development is placed below `admin` and retains the
`pynchy-dev` profile's admin and host-execution settings. Category membership
never grants a semantic child the category's broader policy.

## Config Merging

Workspace specs come from plugins and layered workspace documents. When both
define the same workspace folder, **personalized config wins**.

```
Plugin provides:   workspace + profile config
User overrides:    data/personalization/workspaces/same-folder.toml
Result:            Personalized config takes priority
```

This lets plugins provide sensible defaults while users retain full control.

## Reconciliation

Scheduled-task definitions and run evidence live in the database, but the **source of truth is the personalization tree** (and plugin specs). Temporal owns future fire times, delayed one-shots, retries, and execution state. On every startup, `reconcile_workspaces()` syncs the declared config into the database:

1. Merges plugin specs with layered `workspaces/*.toml` documents
2. Creates chat groups for workspaces missing database entries
3. Creates or updates scheduled tasks from `automations/*/config.toml`
4. Creates channel aliases across messaging platforms

### Automatic config-to-database sync

For file-backed jobs, the reconciler compares the database row against the automation document on every startup. If any of these fields differ, it patches the database to match:

- **`schedule`** — reconciles the Temporal Schedule; Temporal derives the next fire time
- **`prompt`** — updates the prompt sent to the agent on each scheduled run
- **delivery chat** — follows the target workspace's current registered JID
- **`repo`** — updates the repo worktree mounts from the selected profiles

Each scheduled run resolves the target workspace's current effective model. To
change a schedule or prompt, edit its automation file. To change a repo mount
or model override, edit the workspace TOML. Pynchy applies each
valid edit using the [field-specific configuration refresh
mechanism](../usage/personalization.md#apply-configuration-changes). No manual
database edits are required.

## Workspace Config Fields

| Field | Type | Description |
|-------|------|-------------|
| `profiles` | `list[str]` | Profile names from `[profiles.*]`, applied in order |
| `soul` | `str` | Optional `souls/*` prompt ID; defaults to `[prompts].default_soul` |
| `pipeline` | `str` | Optional named pipeline; defaults to `[prompts].default_pipeline` |
| `model` | `str` | Optional model override; takes precedence over profile and global agent models |
| `threads` | `list[table]` | Durable child threads; add `workspace` and `profiles` for a semantic policy owner |
| `scopes` | `list[table]` | Semantic policy owners physically placed below this root without a static child thread |

## Profile Config Fields

| Field | Type | Description |
|-------|------|-------------|
| `includes` | `list[str]` | Profiles to compose before this profile |
| `prompts` | `list[str]` | Prompt names to include |
| `skills` | `list[str]` | Skill names to include |
| `denied_skills` | `list[str]` | Personalized skill names blocked for this profile, even when a selected tier would otherwise include them |
| `tools` | `list[str]` | Tool names from `[tools.*]` to select |
| `repo` | `list[str]` or `str` | GitHub slug (`owner/repo`) from `[repos.*]`; mounts project worktrees |
| `model` | `str` | Optional reusable default model; a workspace can override it |
| `execution_mode` | `"container"` or `"host"` | Where the selected agent core runs; defaults to container execution |
| `cwd` | `str` | Working directory for host execution |
| `is_admin` | `bool` | Whether workspaces using this profile get admin privileges |
| `contains_secrets` | `bool` | Whether workspace files may contain secrets |

## Personalized Skill Access

Pynchy exposes the live personalization skill catalog to sessions through
`search_skills(query)`. Skill discovery requests always read this live catalog;
the agent does not infer current availability from prior conversation or its
system prompt. An agent that finds a useful skill calls
`request_skill_access(skill_name, reason)`, which posts one interactive choice:
Grant once, Grant always, Deny once, or Deny always.

One-time grants return the skill instructions to the current turn only. The
always choices update the workspace's first profile: grants are stored in its
`skills` list and denials in `denied_skills`. Pynchy synchronizes that resolved
selection into the next session's skill registry; a denied skill is never
injected, including when `skills = ["learned"]` or `skills = ["*"]` would
otherwise select it. Dynamic Discord threads use their policy owner's
configuration for both the catalog and persistent choice. An unowned manual
thread inherits its physical parent by default.

Companion skills form a separate set: only an available selected tool installs
them. Learned-skill grants and wildcard selection cannot add a companion skill
or its credentials. See [Tool access and secrets](../usage/tool-access.md).

---

**Want to customize this?** Write your own workspace plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
