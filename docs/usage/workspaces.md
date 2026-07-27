# Workspace Configuration

Use profiles and workspaces to give each chat the right instructions, tools,
repositories, and security boundary. A profile describes reusable capabilities;
a workspace selects one or more profiles and optionally binds them to a
configured channel chat.

## Configure a Profile

Put reusable policy in a profile. A workspace can combine several profiles;
list-valued fields merge in profile order, while a later profile wins for a
scalar such as `model`.

```toml
[profiles.project-worker]
prompts = ["base", "project-worker"]
skills = ["core", "code-review"]
tools = ["browser", "gdrive.personal"]
repo = ["owner/project"]
model = "gpt-5.5"
```

Profiles can include other profiles with `includes = ["base-profile"]`.
They also carry security-relevant fields such as `is_admin`,
`contains_secrets`, and capability rules. Keep an admin profile narrowly
scoped: it cannot select a tool marked `public_source = true`. See [Tool
Trust](security.md) for that policy.

Tool declarations own their required environment and companion skills. A
profile selects the tool; it does not grant credentials by naming a skill.
See [Tool access and secrets](tool-access.md).

## Bind a Workspace to a Chat

Select the profile from a workspace and set `chat` when the workspace targets
an existing configured channel or direct message:

```toml
[workspaces.project]
profiles = ["project-worker"]
chat = "connection.discord.mybot.chat.community.channels.project"
```

Chat references follow this shape:

```text
connection.<platform>.<connection-name>.chat.<platform-specific-target>
```

For Discord, use `<guild-key>.channels.<channel-key>` for a configured guild
channel or `direct.<user-key>` for a configured direct message. See
[Discord](../channels/discord.md) for the complete Discord setup.

When `chat` is omitted, Pynchy can create a chat through the configured
[command center](../channels/index.md#command-center) when that channel supports group
creation. Give `chat` explicitly whenever the workspace must bind to a known,
existing conversation.

## Organize Child Conversations

Declare durable child conversations on a workspace root when several topics
need the same profile and access policy. Each child thread gets an isolated
runtime folder but inherits the root workspace's resolved profile. This keeps
the policy boundary at the parent workspace instead of copying profiles into
every conversation.

```toml
[workspaces.relationships]
profiles = ["relationships"]
threads = [
  { name = "family" },
  { name = "family-gardening" },
  { name = "chat-manager" },
]
```

On startup, Pynchy finds a same-named child thread below `relationships` and
registers it, or creates it when the channel supports both child-thread lookup
and creation. Lookup is required before creation so a restart cannot create a
duplicate. The Discord channel supports this arrangement; channels without
idempotent lookup report the thread as blocked and receive no mutation.

`reconcile_workspace_threads(..., dry_run=True)` returns the same proposed
thread actions without creating threads or changing registrations. Use it from
an operator integration before applying a large layout.

Ordinary dynamic threads and scheduled agent jobs inherit the parent
workspace's selected tools. A semantic child workspace resolves its own
profiles and therefore its own tool access.

## Move Existing Roots Safely

Creating a parent workspace does not move messages from an existing Discord
root channel. Keep every legacy root registered while users move to its target
thread and while scheduled tasks still target the old workspace. Record that
mapping before removing the legacy workspace from `[workspaces]`:

```toml
[workspace_migrations.fam]
target_workspace = "relationships"
target_thread = "family"
# Leave both false while the old root remains live.
inbound_retargeted = false
scheduled_jobs_retargeted = false
retire_legacy_workspace = false
```

Set `retire_legacy_workspace = true` only after changing the external inbound
workflow and scheduled-job configuration. Pynchy requires both corresponding
confirmation fields and still refuses retirement if an active stored task
targets the legacy workspace. Retirement unregisters Pynchy from the old root;
it does not delete the Discord channel.

## Workspace Overrides

A workspace can override the resolved profile model without duplicating the
rest of the profile:

```toml
[workspaces.project-fast]
profiles = ["project-worker"]
model = "gpt-5.5-mini"
```

Pynchy reconciles configured workspaces at startup. It creates or updates the
corresponding runtime registration and pauses config-owned jobs that no longer
have a matching definition. For how runtime registrations, dynamic threads, and
plugin-provided workspaces work internally, see [Workspace Architecture](../architecture/workspaces.md).

---

**Want to customize this?** Write a workspace or channel plugin — see the
[Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to
build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
