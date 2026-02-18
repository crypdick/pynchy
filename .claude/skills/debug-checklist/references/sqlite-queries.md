# SQLite Query Reference for Debugging

Database path: `store/messages.db`

**Host access:** If not on the pynchy host directly, prefix commands with `ssh pynchy` (Tailscale). See `.claude/deployment.md`.

## Table Overview

| Table | What it stores | Truncated? |
|-------|---------------|------------|
| `messages` | Full conversation messages (user, assistant, system, host, tool_result) | No |
| `events` | Observer events: agent traces (thinking, tool_use, text), activity, message echoes | Message content truncated to 500 chars |

The `messages` table is the source of truth for conversation content.
The `events` table captures agent internals (thinking, tool calls, system prompts) via the observer plugin.

## Messages Table

### Recent messages in a specific channel

```bash
sqlite3 store/messages.db "
  SELECT timestamp, sender_name, message_type, substr(content, 1, 120) AS preview
  FROM messages
  WHERE chat_jid = '<JID>'
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

### Recent messages across all channels

```bash
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid, sender_name, message_type, substr(content, 1, 80) AS preview
  FROM messages
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

### Full content of a specific message (no truncation)

```bash
sqlite3 store/messages.db "
  SELECT content
  FROM messages
  WHERE id = '<MSG_ID>' AND chat_jid = '<JID>';
"
```

### Messages of a specific type

```bash
# All tool_result messages in a channel
sqlite3 store/messages.db "
  SELECT timestamp, substr(content, 1, 200) AS preview, metadata
  FROM messages
  WHERE chat_jid = '<JID>' AND message_type = 'tool_result'
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

Valid `message_type` values: `user`, `assistant`, `system`, `host`, `tool_result`

### Search message content by substring

```bash
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid, sender_name, message_type, substr(content, 1, 120) AS preview
  FROM messages
  WHERE content LIKE '%<SUBSTRING>%'
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

### System messages containing a substring

```bash
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid, substr(content, 1, 200) AS preview
  FROM messages
  WHERE message_type = 'system' AND content LIKE '%<SUBSTRING>%'
  ORDER BY timestamp DESC
  LIMIT 10;
"
```

## Events Table (Agent Traces)

### Recent tool calls globally

```bash
sqlite3 store/messages.db "
  SELECT e.timestamp, e.chat_jid,
    json_extract(e.payload, '$.tool_name') AS tool,
    substr(e.payload, 1, 200) AS payload_preview
  FROM events e
  WHERE e.event_type = 'agent_trace'
    AND json_extract(e.payload, '$.trace_type') = 'tool_use'
  ORDER BY e.timestamp DESC
  LIMIT 20;
"
```

### Recent tool calls in a specific channel

```bash
sqlite3 store/messages.db "
  SELECT timestamp,
    json_extract(payload, '$.tool_name') AS tool,
    substr(payload, 1, 300) AS payload_preview
  FROM events
  WHERE event_type = 'agent_trace'
    AND json_extract(payload, '$.trace_type') = 'tool_use'
    AND chat_jid = '<JID>'
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

### Agent thinking traces

```bash
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid, substr(json_extract(payload, '$.thinking'), 1, 200) AS thinking
  FROM events
  WHERE event_type = 'agent_trace'
    AND json_extract(payload, '$.trace_type') = 'thinking'
  ORDER BY timestamp DESC
  LIMIT 10;
"
```

### System-type traces (init, prompt assembly, etc.)

```bash
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid,
    json_extract(payload, '$.subtype') AS subtype,
    substr(json_extract(payload, '$.message'), 1, 200) AS message
  FROM events
  WHERE event_type = 'agent_trace'
    AND json_extract(payload, '$.trace_type') = 'system'
  ORDER BY timestamp DESC
  LIMIT 10;
"
```

### Search trace payloads by substring

```bash
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid, event_type, substr(payload, 1, 300) AS payload_preview
  FROM events
  WHERE payload LIKE '%<SUBSTRING>%'
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

### Agent activity timeline (start/stop)

```bash
sqlite3 store/messages.db "
  SELECT timestamp, chat_jid, json_extract(payload, '$.active') AS active
  FROM events
  WHERE event_type = 'agent_activity'
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

## Cross-Table: Full Trace of a Single Agent Run

Combine messages and events for a specific channel in a time window to reconstruct what happened:

```bash
sqlite3 -header store/messages.db "
  SELECT 'msg' AS source, timestamp, message_type AS type, sender_name, substr(content, 1, 100) AS preview
  FROM messages
  WHERE chat_jid = '<JID>' AND timestamp >= '<START_ISO>' AND timestamp <= '<END_ISO>'
  UNION ALL
  SELECT 'evt' AS source, timestamp, event_type AS type, json_extract(payload, '$.trace_type') AS sender_name, substr(payload, 1, 100) AS preview
  FROM events
  WHERE chat_jid = '<JID>' AND timestamp >= '<START_ISO>' AND timestamp <= '<END_ISO>'
  ORDER BY timestamp;
"
```

## Useful Meta Queries

### List all known channels

```bash
sqlite3 store/messages.db "SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC;"
```

### Message volume by channel (last 24h)

```bash
sqlite3 store/messages.db "
  SELECT chat_jid, COUNT(*) AS msg_count
  FROM messages
  WHERE timestamp >= datetime('now', '-1 day')
  GROUP BY chat_jid
  ORDER BY msg_count DESC;
"
```

### Event counts by type

```bash
sqlite3 store/messages.db "
  SELECT event_type, COUNT(*) AS cnt
  FROM events
  GROUP BY event_type
  ORDER BY cnt DESC;
"
```
