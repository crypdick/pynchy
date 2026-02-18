---
name: Debug Checklist
description: Use when debugging Pynchy issues — service not running, agent not responding, container timeouts, WhatsApp auth problems, mount issues, session transcript branching, or inspecting message history and agent traces in the SQLite database.
---

# Pynchy Debug Checklist

## Known Issues (2026-02-08)

### 1. [FIXED] Resume branches from stale tree position
When agent teams spawns subagent CLI processes, they write to the same session JSONL. On subsequent `query()` resumes, the CLI reads the JSONL but may pick a stale branch tip (from before the subagent activity), causing the agent's response to land on a branch the host never receives a `result` for. **Fix**: pass `resumeSessionAt` with the last assistant message UUID to explicitly anchor each resume.

### 2. IDLE_TIMEOUT == CONTAINER_TIMEOUT (both 30 min)
Both timers fire at the same time, so containers always exit via hard SIGKILL (code 137) instead of graceful `_close` sentinel shutdown. The idle timeout should be shorter (e.g., 5 min) so containers wind down between messages, while container timeout stays at 30 min as a safety net for stuck agents.

### 3. Cursor advanced before agent succeeds
`processGroupMessages` advances `lastAgentTimestamp` before the agent runs. If the container times out, retries find no messages (cursor already past them). Messages are permanently lost on timeout.

## Quick Status Check

```bash
# 1. Is the service running?
systemctl --user status pynchy

# 2. Any running containers?
docker ps --filter name=pynchy

# 3. Any stopped/orphaned containers?
docker ps -a --filter name=pynchy

# 4. Recent errors in service log?
journalctl --user -u pynchy -p err -n 20

# 5. Is WhatsApp connected? (look for last connection event)
journalctl --user -u pynchy --grep 'Connected to WhatsApp|Connection closed' -n 5

# 6. Are groups loaded?
journalctl --user -u pynchy --grep 'groupCount' -n 3
```

## Session Transcript Branching

```bash
# Check for concurrent CLI processes in session debug logs
ls -la data/sessions/<group>/.claude/debug/

# Count unique SDK processes that handled messages
# Each .txt file = one CLI subprocess. Multiple = concurrent queries.

# Check parentUuid branching in transcript
uv run python -c "
import json, sys
lines = open('data/sessions/<group>/.claude/projects/-workspace-group/<session>.jsonl').read().strip().split('\n')
for i, line in enumerate(lines):
  try:
    d = json.loads(line)
    if d.get('type') == 'user' and d.get('message'):
      parent = d.get('parentUuid', 'ROOT')[:8]
      content = str(d['message'].get('content', ''))[:60]
      print(f'L{i+1} parent={parent} {content}')
  except: pass
"
```

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
sqlite3 store/messages.db "SELECT chat_jid, MAX(timestamp) as latest FROM messages GROUP BY chat_jid ORDER BY latest DESC LIMIT 5;"
```

## Container Mount Issues

```bash
# Check mount validation logs (shows on container spawn)
grep -E 'Mount validated|Mount.*REJECTED|mount' logs/pynchy.log | tail -10

# Verify the mount allowlist is readable
cat ~/.config/pynchy/mount-allowlist.json

# Check group's container_config in DB
sqlite3 store/messages.db "SELECT name, container_config FROM registered_groups;"

# Test-run a container to check mounts (dry run)
# Replace <group-folder> with the group's folder name
container run -i --rm --entrypoint ls pynchy-agent:latest /workspace/extra/
```

## WhatsApp Auth Issues

```bash
# Check if QR code was requested (means auth expired)
grep 'QR\|authentication required\|qr' logs/pynchy.log | tail -5

# Check auth files exist
ls -la store/auth/

# Re-authenticate if needed
uv run pynchy-whatsapp-auth
```

## Inspecting Message History & Agent Traces

For debugging agent behavior, prefer querying the SQLite database over docker logs. Docker logs truncate output, but the `messages` table stores full content and the `events` table captures agent internals (thinking, tool calls, system prompts).

**Host access:** These queries must run where the DB lives. If not on the host directly, prefix with `ssh pynchy` (Tailscale). See `.claude/deployment.md` for remote access patterns.

Database: `store/messages.db`

| Table | What it stores | Notes |
|-------|---------------|-------|
| `messages` | Conversation messages | Full content, not truncated |
| `events` | Agent traces from observer | Payload is JSON; message echoes truncated to 500 chars |

Quick examples:

```bash
# Last 20 messages in a channel
sqlite3 store/messages.db "
  SELECT timestamp, sender_name, message_type, substr(content, 1, 120)
  FROM messages WHERE chat_jid = '<JID>'
  ORDER BY timestamp DESC LIMIT 20;
"

# Last 20 tool calls globally
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid, json_extract(payload, '$.tool_name') AS tool
  FROM events
  WHERE event_type = 'agent_trace'
    AND json_extract(payload, '$.trace_type') = 'tool_use'
  ORDER BY timestamp DESC LIMIT 20;
"

# System messages containing a substring
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid, substr(content, 1, 200)
  FROM messages
  WHERE message_type = 'system' AND content LIKE '%<SUBSTRING>%'
  ORDER BY timestamp DESC LIMIT 10;
"

# List known channels
sqlite3 store/messages.db "SELECT jid, name FROM chats ORDER BY last_message_time DESC;"
```

For the full query cookbook (cross-table traces, thinking traces, activity timelines, payload search, etc.) see [references/sqlite-queries.md](references/sqlite-queries.md).

Docker logs remain useful for runtime errors (container crashes, process failures) where the issue occurs before messages reach the database.

## Service Management

See `.claude/deployment.md` in the project root for service management commands.
