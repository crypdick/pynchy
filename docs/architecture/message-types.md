# Message Types

The message type system that determines how each message is stored, filtered, and displayed. Use this page to debug missing messages, write channel plugins, and trace what the LLM receives in its context.

## Message Types

| Type | Purpose | Stored in DB | Sent to LLM | Channel Display |
|------|---------|--------------|-------------|-----------------|
| `user` | Human messages | ✅ | ✅ | Plain text |
| `assistant` | LLM responses | ✅ | ✅ | With assistant name |
| `system` | Persistent context | ✅ | ✅ | Distinct rendering |
| `tool_result` | Command outputs | ✅ | ✅ | 🔧 prefix + ✅/❌ |
| `host` | Operational notifications | ✅ | ❌ **FILTERED** | 🏠 prefix |

### System Context Types

There are two distinct types of system context:

1. **system_notices** (Ephemeral)
   - Recomputed on each agent run
   - Examples: git warnings, uncommitted changes, deployment state
   - Passed via `ContainerInput.system_notices`
   - Passed separately to the selected agent core as ephemeral context
   - NOT stored in database

2. **message_type='system'** (Persistent)
   - Stored in database as regular messages
   - Part of conversation history
   - Sent to LLM as part of message list
   - For context that should persist across sessions

## Database Schema

```sql
CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    timestamp TEXT,
    is_from_me INTEGER,
    message_type TEXT DEFAULT 'user',
    metadata TEXT,  -- JSON
    PRIMARY KEY (id, chat_jid)
);

CREATE TABLE message_ingestion_order (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    UNIQUE (message_id, chat_jid),
    FOREIGN KEY (message_id, chat_jid) REFERENCES messages(id, chat_jid) ON DELETE CASCADE
);

CREATE INDEX idx_messages_by_chat ON messages(chat_jid, timestamp);
```

### Metadata Field

The `metadata` column stores structured JSON data for additional context:

- **tool_result**: `{"exit_code": 0}`
- Future: tool_use_id, error details, etc.

## Data Flow

### Storage Layer

```python
# Host message (operational)
await store_message_direct(
    message_id="host-123",
    chat_jid="chat@g.us",
    sender="host",
    sender_name="host",
    content="⚠️ Agent error occurred",
    timestamp=datetime.now(UTC).isoformat(),
    is_from_me=True,
    message_type="host",  # Will be filtered out
)

# Tool result (command output)
await store_message_direct(
    message_id="cmd-123",
    chat_jid="chat@g.us",
    sender="command_output",
    sender_name="command",
    content="Command output...",
    timestamp=datetime.now(UTC).isoformat(),
    is_from_me=True,
    message_type="tool_result",
    metadata={"exit_code": 0},
)
```

### Retrieval & Filtering

```python
from pynchy.host.orchestrator.messaging.formatter import format_messages_for_sdk

# Retrieve messages from DB
messages = await get_messages_since(chat_jid, durable_cursor)

# Convert to SDK format (filters out host messages)
sdk_messages = format_messages_for_sdk(messages)
# Host messages are automatically excluded
```

### Container Integration

```python
# Host sends SDK messages and ephemeral system notices
ContainerInput(
    messages=sdk_messages,              # SDK message list (host messages filtered)
    system_notices=["Git warning..."],  # Ephemeral context
    ...
)
```

The container receives `messages` (a list of SDK-format messages with host messages already filtered out) and `system_notices` (ephemeral context appended to the system prompt).

## Key Adapters

### Host notifications

`PynchyApp.broadcast_host_message()` delegates to the shared host notification function:

- Stores the message with `message_type='host'`
- Sends a `HOST` event through the shared channel bus for each channel to render
- Emits an event for operational observers
- Never forwards the message to the LLM

### User message ingestion

Handles user message ingestion:

- Stores the message with `message_type='user'`
- Emits to the event bus
- Broadcasts to all channels
