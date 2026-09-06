# Message Routing

How messages flow from channels to agents and back. Read this to debug message delivery and reason about what the LLM sees in its context. For user-facing info on talking to your assistant (trigger words, message prefixes), see [Usage](../usage/index.md).

Messages arrive from plugin-provided [channels](../channels/index.md) and all flow through the same routing code path.

Authenticated provider events that retain context by immutable external subject
use the separate [routed conversation foundation](conversation-routing.md).

## Chat History And Trace History

SQLite chat history stores channel-visible messages and operational notices.
The built-in SQLite observer also stores bounded, irreversibly redacted tool
and text evidence for live operational review. LiteLLM exports complete LLM
request/response metadata to Phoenix with message bodies disabled. Phoenix
remains the source of truth for token usage, cost, timing, model, and provider
request metadata, but not prompt or response content. Phoenix is not the
application database for channel-visible chat history.

The sender vocabulary in the database:

| `sender` value | Visible to LLM? | Description |
|----------------|-----------------|-------------|
| `host` | No | Pynchy process notifications (boot, deploy, errors) — user-only |
| `bot` | Yes | Agent-core responses (`AssistantMessage`) |
| `command_output` | Yes | Tool/command results stored in DB |
| `system_notice` | No | Ephemeral system notices (not stored in DB) |
| `{channel_jid}` | Yes | Channel user messages — WhatsApp phone JID, `slack:<channel_id>`, etc. (`UserMessage`) |

Read SQLite to understand the chat workflow and review bounded live evidence.
Read Phoenix to reconstruct model and provider timing without message bodies.

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

## Runtime Serialization

Each workspace folder identifies one durable execution runtime. A runtime
target composes that stable identity with the workspace's current chat address.
Provider thread replacement can change the address while the folder, worktree,
provider session, and checkpoint ledger remain the same.

Interactive messages, scheduled tasks, hidden learning work, and interrupted
turn recovery all enter one queue keyed by the stable runtime identity. The
queue prevents those work sources from running concurrently against the same
workspace. Message work takes priority when the queue drains.

Temporal owns interactive retries and follow-up checks. Incoming notifications
signal the chat's workflow, including while an activity is running. Notifications
received during one turn coalesce into one subsequent check. The local queue
runs each awaited operation once and propagates its result or failure to its
owner; it does not schedule retries or detached message runs.

Each interactive operation returns an explicit turn outcome: completed, retry
requested, continue after a safe interrupt, paused, or reset. The workflow uses
that outcome to retry, continue, or finish. Cancelling an awaiting owner removes
its queued work or stops its active process.

## Interrupted Turn Recovery

Pynchy checkpoints an agent turn in SQLite before it invokes the agent runtime. The checkpoint
records the original input boundary, work type, conversation session, and whether any output
reached the user. It also records the original input provenance and any routed-delivery claim
that supplied the input. A completed interactive turn advances its durable message cursor,
completes that routed claim, and removes the checkpoint in the same database transaction.
The cursor follows SQLite-assigned local ingestion order rather than provider
timestamps. Messages that share a provider timestamp or arrive late remain
pending in the order Pynchy stores them. The polling high-water mark advances
only after every known group in the batch reaches its routing boundary.

Each checkpoint has a durable control state:

| State | Meaning |
|-------|---------|
| `active` | The occurrence can run or enter automatic recovery. |
| `pause_requested` | The host consumed a pause command and is stopping the runtime. |
| `paused` | The occurrence is frozen, unclaimed, and waiting for a user reply. |
| `reset_requested` | The host consumed a context reset and is discarding the occurrence. |

The router consumes an exact pause or reset phrase, or a native application
command intent from a channel adapter, as a host message and updates the
checkpoint and durable input cursor in one SQLite transaction. A stopped pause
request becomes `paused` and releases its execution claim.
Before stopping the runtime, pause also removes autonomous work already queued
behind that turn. A queued one-shot task receives its own paused checkpoint, so
activity retries and external reconciliation cannot restart that occurrence.
Pause is a terminal orchestration outcome, so the queue and Temporal do not
emit an error warning or retry the frozen work. Repeated pause commands leave
the same row paused. Pause also writes a durable chat quiet fence, which
survives checkpoint completion and deployment. While fenced, the host drops
system notices and automated routed webhooks before storage or delivery. A
direct human message or provider-authenticated human event removes the fence;
host confirmations such as ⏸️ remain visible.

The next ordinary message atomically attaches its formatted user input to the
paused row, updates the occurrence's end cursor, and restores `active`. The
recovery invocation uses the retained provider session ID, original provenance,
task ID, durable runtime identity, and routed-delivery claim. It resolves the
runtime's current chat address before entering the shared queue, then sends one
continuation instruction followed by every attached guidance message. Only
successful completion advances through that guidance and completes the delivery
claim.

A paused scheduled occurrence remains the task's in-flight row. New triggers
for that task return a paused terminal outcome instead of creating competing
work. A reply in the occurrence's chat or thread starts its dedicated
interrupted-turn workflow. Scheduler completion bookkeeping runs only after
that original occurrence finishes. Canceling a queued recurring occurrence
does not pause its definition, so a later schedule trigger can run normally
and removes the chat quiet fence when it starts.

`reset context` moves an active or paused row to `reset_requested` before
stopping execution. Reset retires any routed-delivery claim, removes the
checkpoint, and clears provider session and chat history without changing the
recurring task definition. Startup finishes incomplete pause and reset
transitions; it never dispatches `paused` or `reset_requested` rows through
automatic recovery.

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
