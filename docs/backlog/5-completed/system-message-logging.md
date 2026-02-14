# Transparent Token Stream ✅ COMPLETED

**Completed**: 2026-02-14

Log the full LLM context to the database so the chat history faithfully represents what the model saw.

## Design Principle

The user should be able to reconstruct the exact LLM context by reading the chat conversation. Every message type is stored and shown. Nothing hidden.

Two key sender types from the harness:
- **`host`**: shown to the **user only** — the LLM never sees these (boot, deploy, errors)
- **`system`**: shown to **both** the LLM and the user — a real conversation turn that signals "the harness is talking, not the user"

Documented in `docs/REQUIREMENTS.md` under "Transparent Token Stream".

## Implementation Summary

### What Was Done

1. **✅ Added message_type parameter to `_broadcast_trace()`**
   - All trace events now stored with correct `message_type`
   - Default is `"assistant"` for most traces

2. **✅ Updated all trace storage calls**
   - `thinking` → `message_type="assistant"`
   - `tool_use` → `message_type="assistant"`
   - `tool_result` → `message_type="assistant"`
   - `system` → `message_type="system"`
   - `result_meta` → `message_type="assistant"`

3. **✅ Verified SQL filters**
   - Trace messages don't trigger agent runs (filtered by sender at SQL level)
   - `get_new_messages()` and `get_messages_since()` only return messages from: WhatsApp JIDs, tui-user, deploy
   - Trace messages are excluded by design (ephemeral within a turn)

4. **✅ LLM context handling**
   - Trace messages are NOT included in future LLM context
   - This is correct: thinking/tool_use/tool_result are ephemeral within a single turn
   - The SDK handles them internally during that turn
   - Only final text responses are included in future context
   - Traces are logged for **transparency** (users can see the full execution trace)

## Architecture

### Message Flow

```
User message
  ↓
Agent starts processing
  ↓
[TRACE: thinking] ← stored with message_type='assistant', sender='thinking'
  ↓
[TRACE: tool_use] ← stored with message_type='assistant', sender='tool_use'
  ↓
[TRACE: tool_result] ← stored with message_type='assistant', sender='tool_result'
  ↓
[TRACE: text response] ← stored with message_type='assistant', sender='bot'
  ↓
[TRACE: result_meta] ← stored with message_type='assistant', sender='result_meta'
```

### Storage vs. Context

| Message Type | Stored? | In Future LLM Context? | Why? |
|--------------|---------|------------------------|------|
| User messages | ✅ | ✅ | Conversation history |
| Final assistant text | ✅ | ✅ | Conversation history |
| Thinking blocks | ✅ | ❌ | Transparency only (ephemeral within turn) |
| Tool use | ✅ | ❌ | Transparency only (ephemeral within turn) |
| Tool results | ✅ | ❌ | Transparency only (ephemeral within turn) |
| System messages | ✅ | ❌ | Transparency only (ephemeral within turn) |
| Result metadata | ✅ | ❌ | Transparency only (cost/usage tracking) |
| Host messages | ✅ | ❌ | User-facing only (operational notifications) |

### SQL Filters

Messages that trigger agent runs are filtered at the SQL level in `get_new_messages()` and `get_messages_since()`:

```sql
WHERE sender LIKE '%@%' OR sender IN ('tui-user', 'deploy')
```

This automatically excludes all trace messages (thinking, tool_use, tool_result, system, result_meta, host).

### Transparent Token Stream Achieved

✅ **All LLM interactions are logged**: thinking, tool use, tool results, system messages, result metadata
✅ **Users can see the full trace**: Complete transparency into what the LLM did
✅ **Ephemeral traces don't pollute context**: Only final responses are included in future LLM context
✅ **Correct message types**: All traces have proper semantic types for filtering and display

## Files Modified

- `src/pynchy/app.py`: Added `message_type` parameter to `_broadcast_trace()` and updated all call sites

## Related Documentation

- `docs/architecture/message-types.md` - Message type architecture
- `docs/REQUIREMENTS.md` - Transparent Token Stream principle
