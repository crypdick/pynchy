# Server Debug Reference

## Container Timeout Investigation

```bash
# Check for recent timeouts
grep -E 'Container timeout|timed out' logs/pynchy.general.log | tail -10

# Check container log files for the timed-out container
ls -lt groups/*/logs/container-*.log | head -10

# Read the most recent container log (replace path)
cat groups/<group>/logs/container-<timestamp>.log

# Check if retries were scheduled and what happened
grep -E 'Scheduling retry|retry|Max retries' logs/pynchy.general.log | tail -10
```

## Agent Not Responding

```bash
# Check if messages are being received from WhatsApp
grep 'New messages' logs/pynchy.general.log | tail -10

# Check if messages are being processed (container spawned)
grep -E 'Processing messages|Spawning container' logs/pynchy.general.log | tail -10

# Check if messages are being piped to active container
grep -E 'Piped messages|sendMessage' logs/pynchy.general.log | tail -10

# Check the queue state — any active containers?
grep -E 'Starting container|Container active|concurrency limit' logs/pynchy.general.log | tail -10

# Check lastAgentTimestamp vs latest message timestamp
sqlite3 data/messages.db "SELECT chat_jid, MAX(timestamp) as latest FROM messages GROUP BY chat_jid ORDER BY latest DESC LIMIT 5;"
```

## Container Mount Issues

```bash
# Check mount validation logs (shows on container spawn)
grep -E 'Mount validated|Mount.*REJECTED|mount' logs/pynchy.general.log | tail -10

# Verify the mount allowlist is readable
cat ~/.config/pynchy/mount-allowlist.json

# Check group's container_config in DB
sqlite3 data/messages.db "SELECT name, container_config FROM registered_groups;"

# Test-run a container to check mounts (dry run)
# Replace <group-folder> with the group's folder name
container run -i --rm --entrypoint ls pynchy-agent:latest /workspace/extra/
```

## Exercising the Message Pipeline

Pynchy does not expose a production HTTP endpoint for injecting user messages. Send a test
message from a real account through the configured channel to exercise channel authentication,
message ingestion, routing, the agent turn, and outbound delivery.

```bash
# Identify the destination JID before sending the test message.
sqlite3 data/messages.db "
  SELECT jid, name, folder
  FROM registered_groups
  ORDER BY name;
"

# After sending from the real channel, inspect the conversation.
sqlite3 data/messages.db "
  SELECT timestamp, sender_name, message_type, substr(content, 1, 120)
  FROM messages
  WHERE chat_jid = '<JID>'
  ORDER BY timestamp DESC
  LIMIT 15;
"
```

This is useful for:
- **Debugging the agent** while preserving the same trust boundary as normal messages.
- **Exercising MCP tools** — send a message like "use the playwright MCP to check ..." to prompt the agent to invoke an MCP tool it wouldn't use unprompted. Handy for verifying MCP server connectivity, tool schemas, or end-to-end behavior.
- **Verifying output delivery** through the configured channel instead of only inspecting host state.

## WhatsApp Auth Issues

```bash
# Check if QR code was requested (means auth expired)
grep 'QR\|authentication required\|qr' logs/pynchy.general.log | tail -5

# Check auth files exist
ls -la data/neonize.db

# Re-authenticate if needed
uv run pynchy-whatsapp-auth
```
