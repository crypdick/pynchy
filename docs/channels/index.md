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
