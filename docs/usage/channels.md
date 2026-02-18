# Channels

This page explains how Pynchy connects to messaging platforms. Channels are how you talk to your assistant — whether through WhatsApp, Slack, or a terminal UI.

Channels are pluggable. The built-in options cover common platforms, but you can add more (Telegram, Discord, etc.) via plugins.

## Multi-Channel Sync

All channels see the same messages. Send a message from WhatsApp, and the response shows up in Slack and the TUI too. This means you can:

- Start a conversation on your phone (WhatsApp) and continue at your desk (TUI or Slack)
- Monitor agent activity from any connected platform
- Use whichever channel is most convenient at the moment

Under the hood, all outbound messages route through a unified broadcast bus. Each channel maps its platform-specific identifiers (WhatsApp phone numbers, Slack channel IDs) to a canonical group ID, so the agent sees one conversation regardless of which channels are connected.

## Built-in: WhatsApp

The primary channel for most users. Uses the neonize library (whatsmeow Python bindings).

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

## Built-in: TUI

A terminal UI client built with Textual. Connects to Pynchy's HTTP/SSE server — no external service needed.

**Usage:**
```bash
uv run pynchy --tui                          # Local
uv run pynchy --tui --host your-server:8484  # Remote (over Tailscale)
```

The TUI is always available — no configuration or extra dependencies required.

## Enabling and Disabling Channels

Channels activate automatically when their dependencies are installed and configured. To disable a specific channel:

```toml
[plugins.slack]
enabled = false
```

If a channel's dependencies aren't installed or its config section is missing, it's silently skipped at startup.

## Default Channel

The default channel determines which platform creates the admin channel on first run:

```toml
[channels]
default = "whatsapp"   # or "slack", "tui"
```

---

**Want to customize this?** Write your own channel plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
