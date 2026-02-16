# Message Routing

This page explains how messages flow from channels to agents and back. Understanding the routing model helps you debug message delivery, configure trigger patterns, and reason about what the LLM sees in its context.

## Transparent Token Stream

The chat history faithfully represents the LLM's token stream. A user reading the conversation can reconstruct the exact contents of the LLM context. Nothing hides; every message type appears visible and distinguishable.

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
| `{channel_jid}` | Yes | Channel user messages — WhatsApp phone JID, `slack:<channel_id>`, etc. (`UserMessage`) |

The goal: if something went wrong, you can reconstruct what the LLM saw by reading the chat.

## Trigger Pattern

Messages must start with the `@Pynchy` prefix (case insensitive, configurable via `ASSISTANT_NAME`). The `TRIGGER_ALIASES` setting (default: `ghost`) also triggers the bot. Examples:

- `@Pynchy what's the weather?` — triggers
- `@pynchy help me` — triggers (case insensitive)
- `Hey @Pynchy` — ignored (trigger not at start)
- `What's up?` — ignored (no trigger)

## Routing Behavior

- All channels send messages through the same code path
- Only messages from registered groups get processed; the router ignores unregistered groups
- All channels stay in sync — continue conversations from any channel, and every channel displays the same message history
- Messages that arrive while a task runs follow escalation rules — see [Messaging During Active Tasks](../usage/index.md#messaging-during-active-tasks)

For how messages are typed and stored, see [Message types](message-types.md).
