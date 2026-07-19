# WhatsApp

Use WhatsApp to talk to Pynchy through a linked device. The built-in channel
uses Neonize, the Python bindings for Whatsmeow.

## Set up WhatsApp

```bash
uv sync --extra whatsapp
uv run pynchy-whatsapp-auth
```

Scan the QR code with WhatsApp on your phone: **Settings → Linked Devices →
Link a Device**. Wait for the authentication confirmation before stopping the
command.

## Capabilities

- Group and self-chat support
- Typing indicators and read receipts
- Streaming responses that update as the agent writes
- Media messages such as images and documents

Linked devices expire after roughly 30 days without activity. Re-run
authentication when WhatsApp disconnects. The admin channel commonly uses your
WhatsApp self-chat.

---

**Want to customize this?** Write your own channel plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
