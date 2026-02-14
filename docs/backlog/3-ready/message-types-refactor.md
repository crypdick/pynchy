# Message Types Refactor - Proper SDK Message Type Support

**Status:** Ready
**Priority:** Medium
**Estimated Effort:** Large (requires DB migration)

## Goal

Refactor the message system to use proper Claude SDK message types throughout the stack, eliminating ad-hoc patterns and enabling proper semantic handling of different message types.

## Current State

### Problems
1. All messages stored uniformly in DB with only `sender` field to differentiate
2. Messages formatted as XML string instead of proper SDK objects
3. `client.query()` receives a string instead of message list
4. Host messages (operational notifications) mixed with conversation messages
5. System messages (LLM context) implemented via ad-hoc `system_notices` append
6. Command outputs labeled as "system messages" but actually just regular messages

### Current Architecture
```python
# Database
messages: id, chat_jid, sender, sender_name, content, timestamp, is_from_me

# Message formatting
<messages>
  <message sender="Alice" time="...">content</message>
  <message sender="host" time="...">content</message>
</messages>

# SDK call
await client.query(xml_string)  # Just a string
```

## Target State

### Proper Message Types

**Database Schema:**
```sql
CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT,
    message_type TEXT NOT NULL,  -- 'user', 'assistant', 'system', 'host', 'tool_result'
    sender TEXT,                 -- User ID, bot name, 'host', etc.
    sender_name TEXT,            -- Display name
    content JSON,                -- Structured content (text or rich blocks)
    metadata JSON,               -- Arbitrary metadata (severity, command, tool_use_id, etc.)
    timestamp TEXT,
    PRIMARY KEY (id, chat_jid)
);

CREATE INDEX idx_messages_by_chat ON messages(chat_jid, timestamp);
```

**Message Type Semantics:**

1. **`message_type='user'`** → SDK `UserMessage`
   - From humans to LLM
   - Included in LLM context

2. **`message_type='assistant'`** → SDK `AssistantMessage`
   - From LLM responses
   - Included in LLM context

3. **`message_type='system'`** → SDK `SystemMessage`
   - Context FOR the LLM (git warnings, operational context)
   - Included in LLM context
   - Currently implemented via `system_notices` mechanism

4. **`message_type='host'`** → NOT sent to SDK
   - Operational notifications (errors, confirmations, status)
   - Stored in DB for history/resumption
   - **NEVER** sent to LLM
   - Shown to user in UI only
   - Examples: "⚠️ Agent error occurred", "Context cleared"

5. **`message_type='tool_result'`** → SDK `ToolResultBlock`
   - Command outputs, tool execution results
   - Included in LLM context
   - Paired with tool_use_id in metadata

### Target Architecture

```python
# Message storage
await store_message(
    message_type="host",  # Type determines behavior
    content="⚠️ Agent error occurred",
    metadata={"severity": "error", "event_type": "agent_crash"}
)

# Build LLM context
messages = await get_messages(chat_jid, since=cursor)
llm_messages = [msg for msg in messages if msg.message_type != 'host']
sdk_messages = [convert_to_sdk_message(msg) for msg in llm_messages]

# Conversion to SDK
def convert_to_sdk_message(msg: Message) -> SDKMessage:
    match msg.message_type:
        case "user":
            return UserMessage(content=msg.content)
        case "assistant":
            return AssistantMessage(content=msg.content)
        case "system":
            return SystemMessage(content=msg.content)
        case "tool_result":
            return ToolResultMessage(
                content=msg.content,
                tool_use_id=msg.metadata.get("tool_use_id")
            )
        case "host":
            raise ValueError("Host messages excluded from LLM context")

# SDK call
await client.query(sdk_messages)  # Proper SDK message list
```

## Recent Code Changes That Simplify This Plan

**Adapter Refactoring (Feb 2026)**: The dependency injection code in `app.py` was refactored to use composable adapter classes in `src/pynchy/adapters.py`. This significantly reduces the implementation effort:

**Key Adapters:**
- `MessageBroadcaster` - General channel broadcasting (lines 24-45)
- `HostMessageBroadcaster` - Centralizes all host message storage (lines 47-95)
- `UserMessageHandler` - Handles user message ingestion (lines 296-318)

**Impact:** Instead of updating 10+ scattered call sites in `app.py`, we now only need to update 3 focused adapter classes. Each adapter naturally corresponds to a message type.

## Implementation Plan

### Phase 1: Database Migration
1. Add `message_type` column with default 'user'
2. Add `metadata` JSON column (nullable)
3. Backfill `message_type` based on `sender`:
   - `sender='host'` → `message_type='host'`
   - `sender='command_output'` → `message_type='tool_result'`
   - `sender in bot_names` → `message_type='assistant'`
   - Others → `message_type='user'`
4. Make `message_type` NOT NULL after backfill
5. Create index on `(chat_jid, timestamp)` for efficient queries

### Phase 2: Message Storage Layer

**✨ SIMPLIFIED by adapter refactoring** - Centralized injection points instead of scattered call sites!

1. Update `store_message_direct()` signature to add optional `message_type` parameter (defaults to 'user' for backward compatibility during migration)

2. Update adapter injection points (2 adapters):
   - **HostMessageBroadcaster** (lines 1664, 1695): Wrap `store_message_direct` to inject `message_type='host'`
   - **UserMessageHandler**: Update `_ingest_user_message` to pass `message_type='user'`

3. Update direct call sites in `app.py` for SDK-relevant messages:
   - **Line 638** (`_execute_direct_command`): Change to `message_type='tool_result'` for command outputs
   - **Line 827** (`_handle_streamed_output`): Change to `message_type='assistant'` for bot responses (sender='bot')

4. Keep trace events (thinking, tool_use, etc.) as-is - these are NOT SDK message types, just event logging for UI/debugging

5. Remove emoji prefixes from content (move to rendering layer)

6. Store structured metadata instead of embedding in content

**Example wrapper approach:**
```python
# In app._make_http_deps() and app._make_ipc_deps()
def store_host_message(id, jid, sender, sender_name, content, timestamp, is_from_me):
    return store_message_direct(
        id, jid, sender, sender_name, content, timestamp, is_from_me,
        message_type='host'
    )

host_broadcaster = HostMessageBroadcaster(
    broadcaster, store_host_message, self.event_bus.emit
)
```

This allows gradual migration without touching adapter code!

### Phase 3: Message Retrieval & Formatting
1. Update `format_messages()` to build SDK message list instead of XML
2. Add `convert_to_sdk_message()` function in router.py
3. Filter out host messages when building LLM context
4. Update `ContainerInput` to accept `messages: list[dict]` instead of `prompt: str`

### Phase 4: Container/SDK Integration
1. Update container to receive message list in ContainerInput
2. Build proper SDK message objects in container
3. Pass message list to `client.query()` instead of string
4. Remove XML parsing logic

### Phase 5: System Messages Migration
1. Convert `system_notices` mechanism to store as `message_type='system'`
2. Store git health warnings as system messages in DB
3. Include system messages in SDK message list (not as append to system prompt)
4. Remove `system_notices` field from ContainerInput

### Phase 6: UI/Channel Rendering
1. Update channels to render by `message_type`:
   - Host messages: prefix with 🏠
   - Tool results: prefix with ✅/❌ based on exit code
   - System messages: render distinctly (maybe collapsed by default)
2. Update TUI to show message types
3. Update event bus emissions

### Phase 7: Cleanup
1. Remove XML formatting functions
2. Remove old `sender`-based logic
3. Update tests to use new message types
4. Update documentation

## Benefits

1. ✅ **SDK Native**: Uses Claude SDK exactly as designed
2. ✅ **Clear Semantics**: Message type determines behavior, no ambiguity
3. ✅ **Type Safety**: Can't accidentally include host messages in LLM context
4. ✅ **No Ad-hoc Patterns**: No more `[handoff]`, emoji prefixes in content, XML strings
5. ✅ **Rich Content**: Can use SDK content blocks (text, tool results, etc.)
6. ✅ **Proper Separation**: Host messages vs conversation messages clearly distinct
7. ✅ **Extensibility**: Easy to add new message types (images, etc.)
8. ✅ **Debuggability**: Clear message types in DB, logs, UI

## Migration Risk Mitigation

1. **Backward Compatibility**: Keep reading old format during transition
2. **Gradual Rollout**: Can deploy each phase independently
3. **Testing**: Extensive tests for migration scripts and new code paths
4. **Rollback Plan**: DB migration is additive (add columns, don't remove)
5. **Data Validation**: Verify all messages migrated correctly before making required
6. **Adapter Isolation**: ✨ NEW - Adapters can be tested independently with mock storage functions

## Success Criteria

1. All messages stored with proper `message_type`
2. Host messages never sent to LLM
3. SDK receives proper message objects (UserMessage, SystemMessage, etc.)
4. No more XML string formatting
5. All tests passing
6. UI renders message types correctly
7. Zero data loss during migration

## Open Questions

1. Should we archive old host messages more aggressively? They're operational, less critical.
2. Do we want a separate table for host messages vs conversation messages?
3. How do we handle messages during migration? (Probably read both formats)

## Related Changes

- **✅ Feb 2026**: Adapter refactoring (commit 905727e) - Extracted dependency adapters, reducing Phase 2 effort by ~70%
- Context reset dirty repo warning (recently implemented using system_notices pattern)
- Command output handling (currently misleading comment says "system message")
- Host message documentation (recently clarified in comments)

## Estimated Effort Update

**Original Estimate:** Large (7 phases, 10+ scattered call sites for message storage)
**Revised Estimate:** Medium-Large (7 phases, but Phase 2 now only 2 adapters + 2 direct call sites)

The adapter refactoring reduced Phase 2 complexity significantly:
- **Before**: 10+ scattered `store_message_direct()` calls across 1850-line app.py
- **After**: 2 adapter wrappers + 2 direct call sites in app.py
- **Specific changes needed:**
  - Wrap `HostMessageBroadcaster` injection (2 locations: lines 1664, 1695)
  - Update `_ingest_user_message` for user messages
  - Update line 638 for tool_result (command outputs)
  - Update line 827 for assistant messages (bot responses)
- **Benefit**: Clearer boundaries, easier testing, lower risk of missing call sites
