# Message Routing

How messages flow from channels to agents and back. Read this to debug message delivery and reason about what the LLM sees in its context. For user-facing info on talking to your assistant (trigger words, message prefixes), see [Usage](../usage/index.md).

Messages arrive from plugin-provided [channels](../channels/index.md) and all flow through the same routing code path.

Authenticated provider events that retain context by immutable external subject
use the separate [routed conversation foundation](conversation-routing.md).

## Chat History And Trace History

SQLite chat history stores channel-visible messages and operational notices.
The built-in SQLite observer also stores bounded, irreversibly redacted tool
and text evidence for live operational review. LiteLLM exports complete LLM
request/response traces to Phoenix, which remains the source of truth for model
trace bodies, token usage, cost, and provider request metadata. Phoenix is not
the application database for channel-visible chat history.

The sender vocabulary in the database:

| `sender` value | Visible to LLM? | Description |
|----------------|-----------------|-------------|
| `host` | No | Pynchy process notifications (boot, deploy, errors) — user-only |
| `bot` | Yes | Agent-core responses (`AssistantMessage`) |
| `command_output` | Yes | Tool/command results stored in DB |
| `system_notice` | No | Ephemeral system notices (not stored in DB) |
| `{channel_jid}` | Yes | Channel user messages — WhatsApp phone JID, `slack:<channel_id>`, etc. (`UserMessage`) |

Read SQLite to understand the chat workflow and review bounded live evidence.
Read Phoenix to reconstruct complete model and provider traces.

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
reached the user. It also records the original input provenance and any routed-delivery claim
that supplied the input. A completed interactive turn advances its durable message cursor,
completes that routed claim, and removes the checkpoint in the same database transaction.

Startup releases only delivery claims with no surviving in-flight turn, then clears the stopped
process's execution claims. Provider runtimes start before Pynchy dispatches each surviving
checkpoint through a stable recovery workflow. The recovery activity rehydrates the existing
agent session, reapplies the original input provenance, and sends a continuation instruction
that tells the agent to inspect its transcript and workspace, avoid repeating completed side
effects, and finish the original request. Scheduled agent turns use the same checkpoint and
claim mechanism. Temporal activity retries reuse that checkpoint. After the scheduled workflow
exhausts its retries, a cleanup activity removes the checkpoint only when no recovery workflow
has claimed it. The next scheduled occurrence can then start normally, while an active recovery
keeps ownership of unfinished work.

A saved conversation session does not indicate running work. Idle conversations have no
in-flight checkpoint, so a deploy or later restart does not wake them. SQLite remains the source
of truth even when an unexpected process loss prevents Pynchy from writing a deploy continuation
file; that file only carries deploy diagnostics and rollback metadata.

Recovery resumes the durable agent turn, not the stopped Python instruction pointer. Temporal
heartbeats detect a lost host activity, while the checkpoint gives the replacement activity the
semantic information required to continue safely. If Temporal cancels a recovery activity,
Pynchy retains the checkpoint and its routed-delivery claim but releases the activity's execution
claim. Startup recovery or a later interactive trigger can then claim the unfinished turn again;
cancellation never completes its input cursor.
