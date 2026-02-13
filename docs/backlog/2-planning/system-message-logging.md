# Transparent Token Stream

Log the full LLM context to the database so the chat history faithfully represents what the model saw.

## Design Principle

The user should be able to reconstruct the exact LLM context by reading the chat conversation. Every message type is stored and shown. Nothing hidden.

Two key sender types from the harness:
- **`host`**: shown to the **user only** — the LLM never sees these (boot, deploy, errors)
- **`system`**: shown to **both** the LLM and the user — a real conversation turn that signals "the harness is talking, not the user"

Documented in `docs/REQUIREMENTS.md` under "Transparent Token Stream".

## Context

The Claude Agent SDK message types ([docs](https://platform.claude.com/docs/en/agent-sdk/python#message-types)):

```python
Message = UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent
```

- `UserMessage` — user input content
- `AssistantMessage` — Claude's response (text blocks, thinking blocks, tool use blocks, tool result blocks)
- `SystemMessage` — system message with metadata (`subtype` + `data` dict)
- `ResultMessage` — final result with cost, usage, session_id, duration
- `StreamEvent` — partial streaming events (when `include_partial_messages=True`)

### Current state

What pynchy currently persists to SQLite:

| What | Stored? | How |
|------|---------|-----|
| User messages (WhatsApp, TUI) | Yes | `store_message()` on inbound |
| Bot responses (final text) | Yes | `store_message_direct()` after formatting |
| Host notifications (boot, deploy, error) | Yes | `_broadcast_host_message()` |
| Deploy markers | Yes | Synthetic message with `sender='deploy'` |
| System messages | **No** | Not logged anywhere |
| Tool use (name, input) | **No** | Streamed to TUI/channels ephemerally, not stored |
| Tool results | **No** | Not stored |
| Thinking blocks | **No** | Streamed ephemerally |
| ResultMessage (cost, usage) | **No** | Not captured |

## Decisions (resolved)

- **Always log** everything — system messages, tool use, thinking, results
- **Always show** all message types in chat history
- Storage cost is acceptable — transparency is worth it

## Plan

### 1. Log system messages

System messages are harness-to-model turns. They're real conversation turns that the LLM reads but knows came from the harness, not the user. Store with `sender='system'`.


### 2. Log tool use and tool results

Currently `_handle_streamed_output` receives `ContainerOutput` with `type="tool_use"` and emits `AgentTraceEvent` but doesn't persist. Add `store_message_direct()` calls for:

- `tool_use`: store tool name + input as `sender='bot'` (it's part of the assistant turn)
- `tool_result`: if we can capture it from the stream

### 3. Log thinking blocks

Currently streamed as "thinking..." but content not persisted. Store the actual thinking text.

### 4. Log ResultMessage data

Capture cost, usage, duration, session_id from the final `ResultMessage`. Could be a new table or metadata on the last message.

### 5. Update SQL filters

System messages (`sender='system'`) must not trigger agent runs:

```sql
AND sender != 'host' AND sender != 'system'
```

Update `get_new_messages()` and `get_messages_since()`.

### 6. Trace the system prompt assembly

Need to trace: where does pynchy build the full system prompt that Claude sees? Key areas:
- CLAUDE.md loading (per-group + global)
- Dynamic context injection (tasks snapshot, groups snapshot)
- The `ClaudeAgentOptions` or equivalent passed to the container agent runner

## Sender vocabulary (after this work)

| `sender` | Visible to LLM? | What it represents |
|-----------|-----------------|-------------------|
| `system` | Yes | Harness-to-model messages — conversation turns the user can also read |
| `host` | No | Pynchy process notifications (boot, deploy, errors) — user-only |
| `bot` | Yes | Assistant messages (text, tool use, thinking — all parts of the assistant turn) |
| `deploy` | Yes | Deploy continuation markers |
| `tui-user` | Yes | Messages from the TUI client |
| `{phone_jid}` | Yes | WhatsApp user messages |
