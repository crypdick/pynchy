# Server Debug Reference

## Container Timeout Investigation

```bash
# Check for recent timeouts
grep -E 'Container timeout|timed out' logs/pynchy.log | tail -10

# Check container log files for the timed-out container
ls -lt groups/*/logs/container-*.log | head -10

# Read the most recent container log (replace path)
cat groups/<group>/logs/container-<timestamp>.log

# Check if retries were scheduled and what happened
grep -E 'Scheduling retry|retry|Max retries' logs/pynchy.log | tail -10
```

## Agent Not Responding

```bash
# Check if messages are being received from WhatsApp
grep 'New messages' logs/pynchy.log | tail -10

# Check if messages are being processed (container spawned)
grep -E 'Processing messages|Spawning container' logs/pynchy.log | tail -10

# Check if messages are being piped to active container
grep -E 'Piped messages|sendMessage' logs/pynchy.log | tail -10

# Check the queue state — any active containers?
grep -E 'Starting container|Container active|concurrency limit' logs/pynchy.log | tail -10

# Check lastAgentTimestamp vs latest message timestamp
sqlite3 data/messages.db "SELECT chat_jid, MAX(timestamp) as latest FROM messages GROUP BY chat_jid ORDER BY latest DESC LIMIT 5;"
```

## Container Mount Issues

```bash
# Check mount validation logs (shows on container spawn)
grep -E 'Mount validated|Mount.*REJECTED|mount' logs/pynchy.log | tail -10

# Verify the mount allowlist is readable
cat ~/.config/pynchy/mount-allowlist.json

# Check group's container_config in DB
sqlite3 data/messages.db "SELECT name, container_config FROM registered_groups;"

# Test-run a container to check mounts (dry run)
# Replace <group-folder> with the group's folder name
container run -i --rm --entrypoint ls pynchy-agent:latest /workspace/extra/
```

## Sending Messages via the TUI API

The TUI HTTP API can be used to send messages to any group without a TUI client. Messages sent this way go through the full pipeline (stored, broadcast to channels, trigger agent) — identical to a real user message.

```bash
# 1. Look up the group's JID
curl -s http://pynchy-server:8484/api/groups | python3 -m json.tool
# Returns: [{"name": "my-group", "jid": "...", ...}, ...]

# 2. Send a message to the agent in that group
curl -s -X POST http://pynchy-server:8484/api/send \
  -H 'Content-Type: application/json' \
  -d '{"jid": "<jid-from-step-1>", "content": "your message here"}'

# 3. Watch the response via SSE (or just check /api/messages after a moment)
curl -s "http://pynchy-server:8484/api/messages?jid=<jid>&limit=5" | python3 -m json.tool
```

This is useful for:
- **Debugging the agent** from an SSH session without needing WhatsApp or the TUI app running.
- **Exercising MCP tools** — send a message like "use the playwright MCP to check ..." to prompt the agent to invoke an MCP tool it wouldn't use unprompted. Handy for verifying MCP server connectivity, tool schemas, or end-to-end behavior.
- **Scripting interactions** from another agent's container (via `mcp__pynchy__*` tools) or CI.

## WhatsApp Auth Issues

```bash
# Check if QR code was requested (means auth expired)
grep 'QR\|authentication required\|qr' logs/pynchy.log | tail -5

# Check auth files exist
ls -la data/neonize.db

# Re-authenticate if needed
uv run pynchy-whatsapp-auth
```
