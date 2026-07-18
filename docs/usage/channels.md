# Channels

Channels are how you talk to your assistant — WhatsApp, Slack, Discord, or a
terminal UI. They're pluggable; built-ins cover those platforms, and you can
add more through plugins.

## Multi-Channel Sync

All channels see the same messages. Send from WhatsApp, the response also shows up in Slack and the TUI. So you can:

- Start a conversation on your phone (WhatsApp) and continue at your desk (TUI or Slack)
- Watch agent activity from any connected platform
- Use whichever channel is convenient

Outbound messages route through a single broadcast bus. Each channel maps its platform-specific identifiers (WhatsApp phone numbers, Slack channel IDs) to a canonical group ID, so the agent sees one conversation no matter which channels are connected.

## Built-in: WhatsApp

The primary channel for most users. Uses neonize (whatsmeow Python bindings).

**Setup:**
```bash
uv sync --extra whatsapp        # Install WhatsApp dependencies
uv run pynchy-whatsapp-auth     # Scan QR code to link your phone
```

**Features:**

- Group and self-chat support
- Typing indicators and read receipts
- Streaming responses (updates in-place as the agent types)
- Media messages (images, documents)

**Notes:**

- WhatsApp linked devices expire after ~30 days of inactivity — re-run auth if disconnected
- The admin channel is typically your WhatsApp self-chat (private messages to yourself)

## Built-in: Slack

Connects via Slack's Socket Mode using the Bolt library. Maps Slack channels and DMs to Pynchy groups.

**Setup:**

1. Create a Slack app with Socket Mode enabled
2. Add bot token and app token environment variable names to `config.toml`:

```toml
[connections.synapse]
type = "slack"
bot_token_env = "SLACK__BOT_TOKEN"
app_token_env = "SLACK__APP_TOKEN"
```

3. Install dependencies:
```bash
uv sync --extra slack
```

**Features:**

- Channel and DM support
- Slack Assistant API panel integration
- Streaming message updates (edits messages in-place)
- Markdown formatting

## Built-in: Discord

Connects as a bot over Discord's official gateway (via `discord.py`). Maps guild
channels, threads, and DMs to Pynchy conversations. Unlike Slack's flat model, one
guild `#channel` can host many threads — each thread is its own conversation but
inherits its parent channel's access config.

**Setup:**

1. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications).
   Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**
   (required to read message text).
2. Invite the bot with the `bot` scope and at least *View Channels*, *Send
   Messages*, *Send Messages in Threads*, *Read Message History*, *Add
   Reactions*, *Connect*, and *Speak* permissions. Do not grant Administrator;
   Pynchy does not need it.
3. Choose stable config names for the server, channels, and users. Pynchy looks
   up matching Discord names at startup, creates missing configured guild
   channels, and stores Discord's raw IDs in runtime state.
4. Put the bot token in an environment variable on the host that runs Pynchy.
   Never put the token value in `config.toml`; reference the variable name
   instead:

```bash
DISCORD_BOT_TOKEN=<bot token>
```

5. Configure the Discord connection and the workspaces that should receive
   Discord messages:

```toml
[connections.mybot]
type = "discord"
bot_token_env = "DISCORD_BOT_TOKEN"   # name of the env var holding the token
dm_policy = "allowlist"               # open | allowlist | disabled
allow_from = ["alice"]                # DM allowlist by Discord display/user name
group_policy = "allowlist"            # open | disabled | allowlist

[connections.mybot.chat.pynchy]
name = "Pynchy"                       # Discord server name
users = ["alice"]                     # voice and text member allowlist

[connections.mybot.chat.pynchy.channels.general]
name = "General"                      # existing Discord voice channel name
kind = "voice"

[workspaces.discord-general]
profiles = ["pynchy-dev"]
chat = "connection.discord.mybot.chat.pynchy.channels.general"

[workspaces.discord-admin]
profiles = ["pynchy-dev"]

[workspaces.discord-dm]
profiles = ["pynchy-dev"]
```

Set `profiles` on Discord workspaces the same way you set it on Slack or TUI
workspaces. Repo-backed agent cores need a profile with `repo = "owner/repo"`.
After startup reconciliation, Pynchy stores the concrete Discord channel or DM
identifier in workspace state as `discord:channel:<id>`, `discord:voice:<id>`,
or `discord:direct:<id>`. Discord threads under a configured text channel become
dynamic isolated contexts and inherit the parent workspace profile.

6. Install dependencies:
```bash
uv sync --extra discord
```

7. Start or reload Pynchy, then check `/status`. The Discord connection should
   appear in `channels`:

```bash
curl -s http://localhost:8485/status
```

If the service reconciled the workspace correctly, `registered_groups` contains
JIDs such as `discord:channel:<channel-id>` and `discord:direct:<user-id>`.
Send a message in the configured channel or DM to confirm inbound delivery.

**Features:**

- Guild channel, thread, and DM support
- Reactions (inbound and outbound)
- Streaming responses (edits a message in-place as the agent types; falls back
  to chunked messages when a reply grows past the 2000-character limit)
- Automatic 2000-character message splitting (preserves code fences)
- Safe mention defaults (never pings `@everyone` unless asked)
- History catch-up after reconnect
- Inbound voice/audio attachment transcription when host speech-to-text is available
- One configured Discord voice workspace with spoken turns and final spoken replies

## Discord Voice Workspace

Pynchy supports one existing voice channel per Discord connection. Configure the
server and channel by name, as in the `General` example above, then bind that
target to a normal workspace. Pynchy never creates a voice channel for this
feature. Its internal ID stays in runtime state, not in the human-edited config.

Joining the configured channel activates a voice session. The Discord
guild/channel user and role allowlists still apply, so a member who cannot send
to the workspace cannot make Pynchy join the room. Pynchy leaves when the last
allowed member leaves. Only final agent responses play as speech; streamed text,
tool traces, and status messages remain silent.

Discord voice needs the `discord` extra, `ffmpeg`, and a system `libopus`
library. It also needs the normal host STT provider described below and the
local Pocket TTS service. Follow [Local speech synthesis](local-speech.md) to
install, operate, update, and remove that service. Pynchy reports its readiness
in the `speech` section of `/status`.

Pocket TTS binds only to `127.0.0.1:8000`. The Discord channel sends final
responses through the configured synthesis provider and writes its WAV response
to a temporary file before FFmpeg re-encodes it to Discord Opus. If synthesis is
unavailable, Pynchy logs the failure and does not fall back to a system voice.

Pynchy loads `libopus` through the system resolver and also checks the usual
Homebrew locations. Set `PYNCHY_DISCORD_OPUS_LIBRARY` to an absolute library
path only when the host installs Opus somewhere else.

DM pairing, interactive question widgets, voice rooms other than the configured
workspace, Discord video, and Stage channels are not supported.

## Inbound Speech-to-Text

Pynchy has shared host-side speech-to-text processing for channel adapters that
can provide audio bytes. Supported inbound audio attachments are cached under
the host data directory, transcribed, and rewritten into the message text before
the agent sees the turn. The original attachment metadata also records the
cached path and transcription status.

The host provider is optional infrastructure. Pynchy first uses the
`faster-whisper` Python package when installed. If that is unavailable, it uses
`PYNCHY_LOCAL_STT_COMMAND` when set, or a `whisper` executable on `PATH`. Set
`PYNCHY_LOCAL_STT_MODEL` and `PYNCHY_LOCAL_STT_LANGUAGE` to tune local provider
defaults. Discord is the first built-in channel wired into this shared service;
other channels need media download support before they can opt in.

## Built-in: TUI

A terminal UI built with Textual. Connects to Pynchy's HTTP/SSE server — no external service needed.

**Usage:**
```bash
uv run pynchy --tui  # Local Unix socket, with loopback TCP fallback
```

The TUI is always available — no config or extra dependencies required. Remote
connections require an explicit public bind and bearer token; see
[Control Plane Access](control-plane.md#enable-remote-tui-access).

## Enabling and Disabling Channels

Channels activate automatically once their dependencies are installed and configured. To turn one off:

```toml
[plugins.slack]
enabled = false
```

If a channel's dependencies aren't installed or its config section is missing, it's silently skipped at startup.

## Command Center

The command center picks which configured connection creates workspaces when
Pynchy needs to provision a channel:

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
