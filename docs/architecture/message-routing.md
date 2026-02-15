# Message Routing

## Transparent Token Stream

The chat history is a faithful representation of the LLM's token stream. A user reading the conversation can reconstruct the exact contents of the LLM context. Nothing is hidden; every message type is visible and distinguishable.

The sender vocabulary in the database:

| `sender` value | Visible to LLM? | Description |
|----------------|-----------------|-------------|
| `system` | Yes | Harness-to-model messages — a conversation turn the user can also read |
| `host` | No | Pynchy process notifications (boot, deploy, errors) — user-only |
| `bot` | Yes | Claude's responses (`AssistantMessage`) |
| `deploy` | Yes | Deploy continuation markers |
| `tui-user` | Yes | Messages from the TUI client (`UserMessage`) |
| `command_output` | Yes | Tool/command results stored in DB |
| `thinking` | Stored | Claude's thinking traces (internal, stored for debugging) |
| `tool_use` | Stored | Tool invocation records (internal) |
| `tool_result` | Stored | Tool result records (internal) |
| `result_meta` | Stored | Result metadata (internal) |
| `system_notice` | No | Ephemeral system notices (not stored in DB) |
| `{phone_jid}` | Yes | WhatsApp user messages (`UserMessage`) |

The goal: if something went wrong, you can reconstruct what the LLM saw by reading the chat.

## Trigger Pattern

Messages must start with the `@Pynchy` prefix (case insensitive, configurable via `ASSISTANT_NAME`). The `TRIGGER_ALIASES` setting (default: `ghost`) also triggers the bot:

- `@Pynchy what's the weather?` — triggers
- `@pynchy help me` — triggers (case insensitive)
- `Hey @Pynchy` — ignored (trigger not at start)
- `What's up?` — ignored (no trigger)

## Routing Behavior

- All channels send messages to the same code path
- Only messages from registered groups are processed; unregistered groups are ignored
- All channels are kept in sync — ongoing conversations can be continued from different channels, and all channels display the same message history

For how messages are typed and stored, see [Message types](message-types.md).
