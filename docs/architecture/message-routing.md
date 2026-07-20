# Message Routing

How messages flow from channels to agents and back. Read this to debug message delivery and reason about what the LLM sees in its context. For user-facing info on talking to your assistant (trigger words, message prefixes), see [Usage](../usage/index.md).

Messages arrive from plugin-provided [channels](../channels/index.md) (WhatsApp, Slack, TUI, etc.) and all flow through the same routing code path.

Authenticated provider events that retain context by immutable external subject
use the separate [routed conversation foundation](conversation-routing.md).

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
| `bot` | Yes | Agent-core responses (`AssistantMessage`) |
| `tui-user` | Yes | Messages from the TUI client (`UserMessage`) |
| `command_output` | Yes | Tool/command results stored in DB |
| `system_notice` | No | Ephemeral system notices (not stored in DB) |
| `{channel_jid}` | Yes | Channel user messages — WhatsApp phone JID, `slack:<channel_id>`, etc. (`UserMessage`) |

The goal: read SQLite to understand the chat workflow, and read Phoenix to
reconstruct model/provider traces.

## Trigger Pattern

Messages must start with the trigger prefix (default `@pynchy`, case
insensitive). Configure the primary name with `[agent].name` and aliases with
`[agent].trigger_aliases`; environment overrides use
`AGENT__NAME` and `AGENT__TRIGGER_ALIASES`. The prefix is stripped before the
message reaches the agent.

## Routing Behavior

- Only messages from registered groups get processed; the router ignores unregistered groups
- All channels stay in sync — see [Channels](../channels/index.md) for how multi-channel broadcast works
- Messages that arrive while a task runs follow escalation rules — see [Messaging During Active Tasks](../usage/index.md#messaging-during-active-tasks)

For how messages are typed and stored, see [Message types](message-types.md).

## Interrupted Turn Recovery

Pynchy checkpoints an agent turn in SQLite before it invokes the agent runtime. The checkpoint
records the original input boundary, work type, conversation session, and whether any output
reached the user. A completed interactive turn advances its durable message cursor and removes
the checkpoint in the same database transaction.

Startup clears claims left by the stopped process before starting the Temporal worker. It then
dispatches each surviving checkpoint through a stable recovery workflow. The recovery activity
rehydrates the existing agent session and sends a continuation instruction that tells the agent
to inspect its transcript and workspace, avoid repeating completed side effects, and finish the
original request. Scheduled agent turns use the same checkpoint and claim mechanism.

A saved conversation session does not indicate running work. Idle conversations have no
in-flight checkpoint, so a deploy or later restart does not wake them. SQLite remains the source
of truth even when an unexpected process loss prevents Pynchy from writing a deploy continuation
file; that file only carries deploy diagnostics and rollback metadata.

Recovery resumes the durable agent turn, not the stopped Python instruction pointer. Temporal
heartbeats detect a lost host activity, while the checkpoint gives the replacement activity the
semantic information required to continue safely.
