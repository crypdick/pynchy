# Message Types

This page documents the message type system that determines how each message gets stored, filtered, and displayed. Understanding these types helps you debug missing messages, write channel plugins, and trace what the LLM receives in its context.

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
   - Container appends to SDK `system_prompt` parameter
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
    id="host-123",
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
    id="cmd-123",
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
from pynchy.router import format_messages_for_sdk

# Retrieve messages from DB
messages = await get_messages_since(chat_jid, since_timestamp)

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

### HostMessageBroadcaster

Handles operational notifications:
- Stores the message with `message_type='host'`
- Broadcasts to channels with 🏠 emoji
- Emits an event for the TUI
- Never forwards the message to the LLM

### UserMessageHandler

Handles user message ingestion:
- Stores the message with `message_type='user'`
- Emits to the event bus
- Broadcasts to all channels
