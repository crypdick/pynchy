# Message Routing

How messages flow from channels to agents and back. Read this to debug message delivery and reason about what the LLM sees in its context. For user-facing info on talking to your assistant (trigger words, message prefixes), see [Usage](../usage/index.md).

Messages arrive from plugin-provided [channels](../usage/channels.md) (WhatsApp, Slack, TUI, etc.) and all flow through the same routing code path.

## Chat History And Trace History

SQLite chat history stores channel-visible messages and operational notices.
LiteLLM exports LLM request/response traces to Phoenix, which is the source of
truth for model trace bodies, tool-call traces, token usage, cost, and provider
request metadata. Phoenix is not the application database for channel-visible
chat history.

The sender vocabulary in the database:

| `sender` value | Visible to LLM? | Description |
|----------------|-----------------|-------------|
| `host` | No | Pynchy process notifications (boot, deploy, errors) — user-only |
| `bot` | Yes | Claude's responses (`AssistantMessage`) |
| `deploy` | Yes | Deploy continuation markers |
| `tui-user` | Yes | Messages from the TUI client (`UserMessage`) |
| `command_output` | Yes | Tool/command results stored in DB |
| `system_notice` | No | Ephemeral system notices (not stored in DB) |
| `{channel_jid}` | Yes | Channel user messages — WhatsApp phone JID, `slack:<channel_id>`, etc. (`UserMessage`) |

The goal: read SQLite to understand the chat workflow, and read Phoenix to
reconstruct model/provider traces.

## Trigger Pattern

Messages must start with the trigger prefix (default `@Pynchy`, case insensitive, configurable via `ASSISTANT_NAME`). The `TRIGGER_ALIASES` setting also triggers the bot. The prefix is stripped before the message reaches the agent.

## Routing Behavior

- Only messages from registered groups get processed; the router ignores unregistered groups
- All channels stay in sync — see [Channels](../usage/channels.md) for how multi-channel broadcast works
- Messages that arrive while a task runs follow escalation rules — see [Messaging During Active Tasks](../usage/index.md#messaging-during-active-tasks)

For how messages are typed and stored, see [Message types](message-types.md).
