# Server Debug Reference

Set `PYNCHY_APP_LOG` to the current application log before using these commands.
macOS launchd writes structured output to `logs/pynchy.stdout.log`; isolated
runtime harnesses use `logs/pynchy.general.log`.

```bash
export PYNCHY_APP_LOG=logs/pynchy.stdout.log
```

## Container Timeout Investigation

```bash
# Check for recent timeouts
grep -E 'Container timeout|timed out' "$PYNCHY_APP_LOG" | tail -10

# Check container log files for the timed-out container
ls -lt groups/*/logs/container-*.log | head -10

# Read the most recent container log (replace path)
cat groups/<group>/logs/container-<timestamp>.log

# Check if retries were scheduled and what happened
grep -E 'Scheduling retry|retry|Max retries' "$PYNCHY_APP_LOG" | tail -10
```

## Agent Not Responding

```bash
# Check if messages are being received from WhatsApp
grep 'New messages' "$PYNCHY_APP_LOG" | tail -10

# Check if messages are being processed (container spawned)
grep -E 'Processing messages|Spawning container' "$PYNCHY_APP_LOG" | tail -10

# Check if messages are being piped to active container
grep -E 'Piped messages|sendMessage' "$PYNCHY_APP_LOG" | tail -10

# Check the queue state — any active containers?
grep -E 'Starting container|Container active|concurrency limit' "$PYNCHY_APP_LOG" | tail -10

# Check lastAgentTimestamp vs latest message timestamp
sqlite3 data/messages.db "SELECT chat_jid, MAX(timestamp) as latest FROM messages GROUP BY chat_jid ORDER BY latest DESC LIMIT 5;"
```

## Container Mount Issues

```bash
# Check mount validation logs (shows on container spawn)
grep -E 'Mount validated|Mount.*REJECTED|mount' "$PYNCHY_APP_LOG" | tail -10

# Verify the mount allowlist is readable
cat ~/.config/pynchy/mount-allowlist.json

# Check group's container_config in DB
sqlite3 data/messages.db "SELECT name, container_config FROM registered_groups;"

# Test-run a container to check mounts (dry run)
# Replace <group-folder> with the group's folder name
container run -i --rm --entrypoint ls pynchy-agent:latest /workspace/extra/
```

## Exercising the Message Pipeline

For routine conversational canaries, use the local control-plane route documented in
[Send synthetic Discord canary input](../../../../docs/usage/control-plane.md#send-synthetic-discord-canary-input).
It reuses the existing Discord bot round trip, strips the canary prefix, and enters the normal
user-message path. Use it to test message routing, the agent turn, tools, and outbound delivery.
Do not open Discord in a browser merely to submit a test prompt.

Use a real non-bot sender only when the test specifically covers Discord's human-authentication,
mention, or access-policy boundary. Synthetic input does not prove that boundary.

```bash
# Identify the destination JID before sending the test message.
sqlite3 data/messages.db "
  SELECT jid, name, folder
  FROM registered_groups
  ORDER BY name;
"

# After sending either kind of message, inspect the conversation.
sqlite3 data/messages.db "
  SELECT timestamp, sender_name, message_type, substr(content, 1, 120)
  FROM messages
  WHERE chat_jid = '<JID>'
  ORDER BY timestamp DESC
  LIMIT 15;
"
```

This is useful for:
- **Debugging the agent** through the same user-message processing path.
- **Exercising MCP tools** — send a message like "use the playwright MCP to check ..." to prompt the agent to invoke an MCP tool it wouldn't use unprompted. Handy for verifying MCP server connectivity, tool schemas, or end-to-end behavior.
- **Verifying output delivery** through the configured channel instead of only inspecting host state.

## WhatsApp Auth Issues

```bash
# Check if QR code was requested (means auth expired)
grep 'QR\|authentication required\|qr' "$PYNCHY_APP_LOG" | tail -5

# Check auth files exist
ls -la data/neonize.db

# Re-authenticate if needed
uv run pynchy-whatsapp-auth
```
