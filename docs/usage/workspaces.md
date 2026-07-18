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
[Channels](channels.md#built-in-discord) for the complete Discord setup.

When `chat` is omitted, Pynchy can create a chat through the configured
[command center](channels.md#command-center) when that channel supports group
creation. Give `chat` explicitly whenever the workspace must bind to a known,
existing conversation.

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
