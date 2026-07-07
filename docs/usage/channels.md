# Channels

Channels are how you talk to your assistant — WhatsApp, Slack, or a terminal UI. They're pluggable; built-ins cover the common platforms, and you can add more (Telegram, Discord, etc.) via plugins.

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
2. Add bot token and app token to `config.toml`:

```toml
[slack]
bot_token = "xoxb-..."
app_token = "xapp-..."
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
2. Invite the bot with the `bot` scope and at least *View Channels*, *Send Messages*,
   *Read Message History*, and *Add Reactions* permissions.
3. Put the bot token in an environment variable (never in `config.toml`), then
   reference the variable name:

```toml
[connection.discord.mybot]
bot_token_env = "DISCORD_BOT_TOKEN"   # name of the env var holding the token
dm_policy = "allowlist"               # open | allowlist | disabled
allow_from = ["discord:<your-user-id>"]  # DM allowlist (user snowflakes); "*" = open
group_policy = "allowlist"            # open | disabled | allowlist

[connection.discord.mybot.chat.<guild-id>]
require_mention = true                 # guild default; require an @mention to respond
users = ["discord:<user-id>"]          # optional sender allowlist
roles = ["role:<role-id>"]             # optional role allowlist

[connection.discord.mybot.chat.<guild-id>.channels.<channel-id>]
enabled = true
require_mention = false                # override the guild default for this channel
```

4. Install dependencies:
```bash
uv sync --extra discord
```

**Features:**

- Guild channel, thread, and DM support
- Reactions (inbound and outbound)
- Streaming responses (edits a message in-place as the agent types; falls back
  to chunked messages when a reply grows past the 2000-character limit)
- Automatic 2000-character message splitting (preserves code fences)
- Safe mention defaults (never pings `@everyone` unless asked)
- History catch-up after reconnect

DM pairing, interactive question widgets, and voice are not yet supported.

## Built-in: TUI

A terminal UI built with Textual. Connects to Pynchy's HTTP/SSE server — no external service needed.

**Usage:**
```bash
uv run pynchy --tui                          # Local
uv run pynchy --tui --host your-server:8484  # Remote (over Tailscale)
```

The TUI is always available — no config or extra dependencies required.

## Enabling and Disabling Channels

Channels activate automatically once their dependencies are installed and configured. To turn one off:

```toml
[plugins.slack]
enabled = false
```

If a channel's dependencies aren't installed or its config section is missing, it's silently skipped at startup.

## Default Channel

The default channel picks which platform creates the admin channel on first run:

```toml
[channels]
default = "whatsapp"   # or "slack", "tui"
```

---

**Want to customize this?** Write your own channel plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
