# Message Types Architecture

This document describes the message type system implemented in Pynchy.

## Overview

Messages in Pynchy are categorized by semantic type, enabling proper handling throughout the stack from storage to SDK integration. This architecture ensures operational notifications (host messages) are never sent to the LLM while maintaining conversation context.

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
    message_type TEXT NOT NULL DEFAULT 'user',
    metadata TEXT,  -- JSON
    PRIMARY KEY (id, chat_jid)
);

CREATE INDEX idx_messages_by_chat ON messages(chat_jid, timestamp);
```

### Metadata Field

The `metadata` column stores structured JSON data for additional context:

- **tool_result**: `{"exit_code": 0}`
- **system notices**: `{"source": "system_notice"}`
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
# Host sends both legacy prompt and SDK messages
ContainerInput(
    prompt=format_messages(messages),  # Legacy XML format
    messages=sdk_messages,              # New SDK format (host filtered)
    system_notices=["Git warning..."],  # Ephemeral context
    ...
)

# Container prefers SDK messages when available
if container_input.messages:
    prompt = build_sdk_messages(container_input.messages)
else:
    prompt = container_input.prompt  # Fallback to legacy

# System notices appended to system_prompt
if container_input.system_notices:
    system_prompt["append"] += "\n\n" + "\n\n".join(system_notices)
```

## Key Adapters

### HostMessageBroadcaster

Handles operational notifications:
- Stores message with `message_type='host'`
- Broadcasts to channels with 🏠 emoji
- Emits event for TUI
- Message NEVER reaches LLM

### UserMessageHandler

Handles user message ingestion:
- Stores message with `message_type='user'`
- Emits to event bus
- Broadcasts to all channels

## Benefits

1. **Type Safety**: Message type determines behavior, no ambiguity
2. **SDK Native**: Uses Claude SDK as designed
3. **Clear Separation**: Host messages vs conversation messages distinct
4. **Extensibility**: Easy to add new message types
5. **Debuggability**: Clear types in DB, logs, UI
6. **Filtered Context**: Host messages never pollute LLM context

## Backward Compatibility

The system maintains full backward compatibility:

- Legacy `prompt` string still supported alongside `messages` list
- XML formatting functions preserved for transition period
- Old databases automatically migrated with backfill
- All existing tests pass without modification

## Future Enhancements

1. **True SDK Message Objects**: When SDK supports passing message lists directly to `query()`, build proper `UserMessage`, `AssistantMessage`, etc. objects instead of converting to XML

2. **Rich Content**: Leverage SDK content blocks for images, files, structured data

3. **Message Metadata**: Store tool_use_id, error details, cost tracking in metadata field

4. **Remove Legacy**: Eventually remove XML formatting when all code paths use SDK messages

## Implementation Status

- ✅ Phase 1: Database migration
- ✅ Phase 2: Storage layer updates
- ✅ Phase 3: Message retrieval & SDK list building
- ✅ Phase 4: Container integration
- ✅ Phase 5: System context documentation
- ✅ Phase 6: UI/channel rendering (emojis at broadcast)
- ✅ Phase 7: Documentation & cleanup

All 465 tests passing. Refactor complete.
