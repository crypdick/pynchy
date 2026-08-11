# Routed Conversations

How authenticated external events join durable agent context and human-facing
control threads. Read this when adding a provider route or debugging duplicate,
out-of-order, or disconnected routed work.

## Identity Boundaries

Pynchy keeps three identities separate:

| Identity | Purpose | Never derived from |
|----------|---------|--------------------|
| External delivery | Deduplicate one authenticated provider event | Mutable title |
| Conversation | Retain context for one immutable external subject | Delivery ID, task ID, chat JID, or title |
| Control binding | Present the conversation in a Discord thread | Conversation session state |

A conversation subject contains a namespace and immutable key. The namespace
includes enough provider, tenant, and route context to prevent two providers or
tenants from claiming the same key. For example, a Linear issue adapter can use
`linear:<tenant>:issue` plus the issue's immutable provider ID.

Workspace placement does not participate in subject uniqueness. Moving a
conversation to another workspace updates policy and presentation placement
while preserving the opaque conversation ID and agent session.

## Authenticated Delivery Admission

The provider-neutral receipt ledger forms the authentication and
delivery-deduplication boundary. Webhook admission writes its richer webhook
receipt and the generic receipt atomically; polling or streaming adapters admit
the same generic receipt after their own authentication checks. Conversation
admission requires that receipt, then links its `(provider, route, delivery_id)`
identity to exactly one conversation. A replayed delivery returns the existing
link. A second delivery for the same subject joins the same conversation, while
a different immutable subject gets a different conversation.

Provider adapters own authentication and subject parsing. Downstream routing
accepts only typed `ExternalDeliveryIdentity` and `ConversationSubject` values,
so unparsed webhook payload shapes do not leak into conversation state.

A conversation receipt can represent either an ordinary prompt-bearing delivery
or lifecycle-only work. Both persist and link through the same immutable delivery
identity. A lifecycle-only entry retains provider-owned durable context, but no
prompt content, so it can update provider or control lifecycle without waking an
agent.

## FIFO Claims And Recovery

Each admitted delivery enters a durable per-conversation FIFO with three states:
`pending`, `claimed`, and `completed`. An atomic claim selects the oldest pending
delivery only when that conversation has no claimed delivery. Different
conversations can hold claims concurrently.

Once ordinary agent execution begins, the in-flight turn durably owns the
delivery claim. An agent or finalization exception retains both records for
semantic recovery; only explicit abandonment returns the delivery to `pending`.
Successful finalization advances the message cursor, removes the turn, and marks
the delivery `completed` in one transaction. After that commit, a process-local
provider callback can claim and inject the next sibling. Callback failure never
rolls back the completed turn; startup scans the durable FIFO again.

A terminal lifecycle delivery never constructs a `NewMessage` or agent turn.
At ingress, it records terminal intent on the durable conversation, clears its
routed session, retires prior routed work and runtime ownership, and archives an
existing Discord control. FIFO delivery preserves audit and retry ordering; it
does not defer those terminal actions. A terminal-first delivery records the
same intent but creates neither a runtime workspace nor a Discord thread.
Runtime retirement removes ephemeral workspace directories and clean worktree
checkouts while retaining Git branch refs. Dirty or untracked worktrees and user
workspace files retain the complete artifact set for recovery.
If a process stops after the durable terminal transition but before local cleanup,
startup repeats that cleanup from the persisted terminal intent.
Each lifecycle delivery also repeats the same idempotent cleanup before its
provider callback completes.

Lifecycle callbacks have at-least-once delivery. An archive or callback failure
keeps lifecycle processing retryable; a process stop after a callback leaves the
remaining durable work for recovery. Route callbacks must make provider side
effects idempotent with the full external delivery identity and tolerate an
already-closed control.

A context reset commits the control thread's clear boundary together with its
conversation state. It removes the routed session and completes pending work,
plus orphan claims without a surviving turn, received at or before that
boundary. Deliveries received after the boundary remain pending and start with
fresh agent context after the reset acknowledgement. The same reset operation
stops the current worker and clears session-scoped security taint.

Startup applies the same clear-boundary repair before returning other orphan
claims to `pending`. Sequence numbers remain unchanged, so a retryable FIFO
head gets claimed again before later deliveries. Claims referenced by surviving
turns stay claimed until those turns resume. An idle conversation carries no
claim.

When an orphaned delivery wakes again, its local message receives the current
wake timestamp. The durable delivery ledger retains the original provider
receipt time and FIFO sequence; reusing that old time in the message table could
place the recovered input behind an already-advanced chat cursor.

Cursor advancement is a separate provider concern. A polling adapter commits
its continuation cursor only after it has validated the whole page and durably
admitted every eligible delivery. Receipt replay repairs a crash between receipt
admission and FIFO linking without creating a second delivery.

## Discord Control Bindings

A control binding stores the current Discord child thread, its explicit parent
workspace and JID, and a human-readable presentation title. Reconciliation calls
the shared `ensure_thread` path every time instead of trusting the stored child
JID. This lookup reuses or unarchives a matching thread; if the thread no longer
exists, reconciliation creates a replacement and updates the binding.

Deleting, renaming, or moving a control thread never changes conversation
identity or agent session. Titles remain readable operator-facing text rather
than protocol-shaped IDs. The registered parent workspace constrains where the
binding can move. Replacing a missing Discord thread uses an explicit atomic
workspace rebind. Ordinary registration rejects duplicate ownership of either
the thread JID or runtime folder.

The control binding is also the authoritative runtime lookup from an exact
thread JID to its opaque conversation ID. Workspace folder names are sanitized
slugs for placement only; they are not reversible identities and must not be
decoded to recover a conversation ID.

The conversation, not its binding, stores provider-neutral terminal intent.
Channels map that intent to their native lifecycle operation; Discord uses thread
archival. A binding mirrors current presentation state and remains the lookup
from a thread JID to its conversation. Terminal reconciliation never calls
`ensure_thread`: Discord lookup can unarchive an archived thread. Startup
reapplies the conversation intent to existing bindings after an interrupted
archive operation.

Terminal intent persists without a binding. Later comments, stale callbacks,
and delayed scheduled work cannot create or unarchive a control while that intent
remains terminal. Only an explicit later nonterminal provider state clears the
intent and permits normal control reconciliation, including runtime registration
for an existing control thread.

## Integration Boundary

This layer provides persistence, typed identities, claims, sessions, and control
reconciliation. Provider plugins still decide how an authenticated event maps to
an immutable subject and when to enqueue agent execution. The generic layer does
not perform provider writes or interpret provider payloads.

## Linear Issue Webhooks

The built-in Linear webhook adapter maps each authenticated `Comment` or `Issue`
event to `linear:<organization-id>:issue` plus the immutable Linear issue ID. An
`Issue/create` callback is recorded as ignored only when its signed callback
state matches the managed `Agent Proposed` state: creating a proposal does not
authorize work. Other issue creations follow the normal callback policy. The
callback state remains immutable for that delivery, so a later state transition
arrives as a separate `Issue/update` delivery. An update whose changed fields
contain `projectId` and only the `addedToProjectAt` and `updatedAt` bookkeeping
fields resolves durable workspace ownership, then completes as an ignored
receipt without an agent turn. Any additional substantive field keeps its
normal callback semantics. The webhook receipt retains the delivery UUID.
Ordinary entries retain only the host-parsed prompt, readable control title,
and control metadata needed to wake the agent; terminal issue entries retain
closed metadata and immutable state evidence for lifecycle-only delivery. Raw
provider shapes do not cross the routing boundary.

An issue update that requests planning, waits for plan approval, or acquires or
confirms an active execution lease is the exception: the Linear integration
records it as controller-owned instead of creating a second conversation
delivery. A signed update whose user actor changed the state directly to `In
Progress` can establish the lease in place. Current provider state alone cannot
establish that authority. The Temporal controller runs planning and execution
in the issue conversation's existing runtime. Comment events and progress
questions join the same ordered thread.

For an `Issue` callback, the adapter reads state ID, display name, and type from
either `data.state` or `data.issue.state`. Only the typed Linear workflow values
`completed` and `canceled` produce terminal lifecycle entries. A display name
such as `Done` never supplies a fallback classification. A typed terminal entry
immediately records terminal conversation intent, clears the routed session,
retires prior routed work, archives an existing control, and never creates an
LLM turn or a terminal-first Discord thread.

At ingress, the terminal entry retains both the callback's parsed state ID and
the managed board's exact `Done` state ID. The Linear lifecycle callback completes
reviewed work only when those immutable values match. `Duplicate`, `Canceled`,
and other typed terminal states cancel the local scheduled task and active
execution without writing a new provider state; they do not complete the work
item. Deleting the issue or moving it off the managed board cannot change this
persisted decision.

Webhook admission commits the immutable receipt, effect candidates, parsed
delivery envelope, initial FIFO state, and any terminal retirement in one SQLite
transaction. A crash cannot leave a receipt without its FIFO entry or terminal
state. Duplicate delivery IDs reuse the transaction's existing receipt and
entry. Separate issue subjects can hold claims concurrently; deliveries for one
issue wait behind its oldest non-completed FIFO head, including a held head.

Before a host-owned Linear comment or nonterminal workflow-state mutation starts,
Pynchy records a provider-neutral outbound-effect intent. A callback with the
same account, event kind, action, and subject is held in the issue FIFO until
the provider response resolves every matching in-flight intent. Exact response
evidence completes the held self callback without an agent turn; a mismatch
releases it in its original FIFO position. Multiple routes and provider retries
use retained confirmation evidence, so one route cannot consume another
route's suppression.

Correlation is classified before route-owned trusted processing. Exact
self-callbacks never run those effects. A candidate callback stores the parsed
event in its held FIFO envelope; after a mismatch releases it, trusted
processing runs idempotently at the FIFO head from that persisted envelope.
Provider deletion or project movement after admission cannot change the event
being processed.

The webhook receipt remains an immutable audit of the authenticated event.
Correlation decisions and candidates are stored separately. A confirmed
comment fingerprint covers comment ID, issue ID, revision, and action; a state
fingerprint covers issue ID, state ID, revision, and action. Pynchy does not
suppress by actor identity, so a person sharing the Linear account remains
actionable once exact evidence disproves the hold. Terminal state callbacks
never enter the hold path because their lifecycle reducer must still close the
control and reconcile managed `Done` work.

If the process loses a mutation after external I/O starts but before exact
response evidence is durable, its effect becomes `outcome_unknown`. Matching
callbacks remain quarantined rather than being guessed safe. Prepared effects
that never reached external I/O are released during startup recovery. Unknown
effects are never silently time-pruned; candidate matching is limited to the
24-hour provider-event window around the attempt, while confirmed evidence is
retained for seven days to cover retries and overlapping routes. The
authenticated control plane lists these records at `GET /webhook-effects`.
After independently proving the provider mutation did not occur, an operator
can send `{"verified_absent": true}` to
`POST /webhook-effects/{effect_id}/reconcile-absent`; that explicit decision
releases held candidates and advances their FIFOs. The record retains a hash of
the requested mutation fields to support that investigation without storing
comment or plan bodies.

The adapter places the stable routed workspace behind a Discord child thread of
the configured Linear workspace. A first callback derives a title such as
`[PYN-123] Repair scheduler`. Later callbacks retain the persisted title and
binding. If Discord no longer has that thread, reconciliation creates a
replacement, moves the runtime workspace to the replacement JID, and rebinds
the conversation's existing agent session.

Planning, execution, follow-ups, retries, recovery, and interactive questions
all use that routed workspace, worktree, provider session, queue, and
checkpoint ledger. A worker process can stop between turns without discarding
the provider session.

Before admission, the host uses the API key from the route's named Linear account
to fetch the current issue and rejects a delivery that does not belong to the
route's workspace Project. The same account declaration controls prompt handling.
A private source sends the parsed comment as trusted conversation input; a public
source fences the same context and starts the invocation corruption-tainted. Both
paths preserve a short, provider-owned wake-up prompt instead of asking the model
to perform board membership checks. Explicit Linear lifecycle actions still
enforce planning and execution workflow state.

The adapter maps Linear workflow state types `completed` and `canceled` to
terminal conversation intent and other typed states to explicit open intent.
Events without a workflow state, such as minimal `Comment` payloads, preserve
terminal intent and cannot revive an archived control. This keeps Linear
authoritative without deriving lifecycle from mutable Discord state.

## Matrix Routes

The first-party Matrix integration applies this foundation to one named owner
identity per `[connections.<name>]` entry. A top-level `[routes.<name>]` binds
one exact Matrix room endpoint to an explicit parent workspace. It never creates
a general Matrix channel or a shared command-center workspace.

For each configured route, startup verifies the joined room, expected owner,
optional bridge name, and optional active-portal state. The connection accepts
only live, decrypted, original `m.room.message` events with `msgtype = "m.text"`
from a sender other than the owner. Backfill, edits, reactions, redactions, and
owner-authored messages do not enter the receipt ledger. An undecryptable live
event fails the page and retains the previous cursor.

`activation = "on_event"` links eligible events to the FIFO and wakes the
conversation. `activation = "on_demand"` records the authenticated event and
advances the connection cursor without creating a FIFO delivery or waking an
agent. Replaying the same Matrix event ID reuses its receipt, conversation, and
delivery.

The generated conversation workspace inherits its configured parent and can
only reduce the parent's tools or capability policy. It receives two
destination-free tools: `matrix_route_read` and `matrix_route_send`. The host
resolves both tools through the active route binding. A caller cannot supply a
room ID or redirect a send. Reads revalidate the current room and portal before
returning history, and sends expose only the provider event ID in their receipt.

The control thread is also the approval surface. Every Matrix send requires
human approval in that thread, bound to the exact connection, route,
conversation, control thread, room, portal assertion, and body. Replay rebuilds
the current workspace policy and rechecks the live portal before transmitting.
See [Matrix communications](../integrations/matrix-gateway.md) for configuration.

For ordinary channel messages and interrupted agent turns, see
[Message routing](message-routing.md). For workspace inheritance below Discord
parents, see [Workspaces](workspaces.md).
