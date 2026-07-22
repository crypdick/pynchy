# Channels

Channels connect Pynchy to the conversations where you work. Built-in channels
cover WhatsApp, Slack, and Discord; plugins can add more.

## Choose a channel

| Channel | Use it when you want to |
|---------|-------------------------|
| [WhatsApp](whatsapp.md) | Talk through a linked personal device. |
| [Slack](slack.md) | Work in Slack channels or DMs. |
| [Discord](discord.md) | Use guild channels, threads, DMs, or a voice workspace. |

For spoken Discord replies and inbound audio transcription, see [Voice and
speech](voice-and-speech.md).

## Multi-channel sync

All channels see the same messages. Send from WhatsApp, and the response also
shows up in Slack and Discord. You can start on your phone, continue at your
desk, or watch agent activity from a different connected channel.

Outbound messages route through one broadcast bus. Each channel maps its
platform-specific identifiers to a canonical group ID, so the agent sees one
conversation regardless of the channel you use.

## Enable or disable channels

Channels activate when their dependencies and configuration exist. To disable a
configured channel, set its plugin to `false`:

```toml
[plugins.slack]
enabled = false
```

If a channel's dependencies are unavailable or its configuration is missing,
Pynchy skips it at startup.

## Inspect source health

Agents can call `messaging_source_health` to inspect current readiness and the
latest inbound timestamp for configured messaging sources. The tool reads
Pynchy-owned runtime state and, when explicitly configured, aggregate metadata
from host-local collector stores. It doesn't read sender identities or message
bodies, open provider conversations, invoke sibling messaging tools, or change
provider read state.

Configure an aggregate metadata root only when Pynchy should project the
body-free WhatsApp and Signal collector records already present on its host:

```toml
[messaging_source_health]
data_dir = "/path/to/host-local/messaging-metadata"
stale_after_hours = 24
```

The root must contain provider-specific `whatsapp/messages.db` and
`signal/messages.db` stores. Pynchy selects only inbound timestamp aggregates
and Signal's last successful receive-check timestamp. Google Messages is a
recognized source, but remains `unavailable` until a body-free durable adapter
covers the complete source; Pynchy doesn't open a browser or substitute a
partial SMS-only view.

Pass connection names or provider types to inspect specific sources. Results
separate metadata availability, collector health, event freshness, and historical
coverage. `latest_inbound` identifies the newest returned source and timestamp
without exposing a sender. The agent tool defaults to WhatsApp, Signal, and
Google Messages when `sources` is omitted, so an unrelated configured channel
can't become the reported latest personal message. Pass a configured connection
name or provider explicitly to inspect another channel. Host-internal IPC callers
that omit `sources` retain the complete configured-plus-personal inventory.
Unknown source names are `not_established`.

## Command center

The command center selects which configured connection creates workspaces when
Pynchy provisions a channel:

```toml
[connections.synapse]
type = "discord"
bot_token_env = "DISCORD_BOT_TOKEN"

[command_center]
connection = "synapse"
```

To route boot, deploy, and shutdown notifications to a predictable admin chat,
set its registered workspace folder:

```toml
[notifications]
admin_workspace = "discord-admin"
```

The configured workspace must be an admin workspace. Without this setting,
Pynchy suppresses host lifecycle notifications.

---

**Want to customize this?** Write your own channel plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
