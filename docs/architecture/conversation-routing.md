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

## FIFO Claims And Recovery

Each admitted delivery enters a durable per-conversation FIFO with three states:
`pending`, `claimed`, and `completed`. An atomic claim selects the oldest pending
delivery only when that conversation has no claimed delivery. Different
conversations can hold claims concurrently.

Once agent execution begins, the in-flight turn durably owns the delivery claim.
An agent or finalization exception retains both records for semantic recovery;
only explicit abandonment returns the delivery to `pending`. Successful
finalization advances the message cursor, removes the turn, and marks the
delivery `completed` in one transaction. After that commit, a process-local
provider callback can claim and inject the next sibling. Callback failure never
rolls back the completed turn; startup scans the durable FIFO again.

A context reset commits the control thread's clear boundary together with its
conversation state. It removes the routed session and completes pending work,
plus orphan claims without a surviving turn, received at or before that
boundary. Deliveries received after the boundary remain pending and start with
fresh agent context after the reset acknowledgement.

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
binding can move.

The control binding is also the authoritative runtime lookup from an exact
thread JID to its opaque conversation ID. Workspace folder names are sanitized
slugs for placement only; they are not reversible identities and must not be
decoded to recover a conversation ID.

The binding also stores provider-neutral closed intent. Channels map that intent
to their native lifecycle operation; Discord uses thread archival. Reconciliation
opens a thread while a routed turn needs it, then successful delivery completion
reapplies the durable intent before waking the next FIFO sibling. Startup
reconciles idle bindings, which repairs a crash after delivery completion but
before the channel lifecycle edit.

## Integration Boundary

This layer provides persistence, typed identities, claims, sessions, and control
reconciliation. Provider plugins still decide how an authenticated event maps to
an immutable subject and when to enqueue agent execution. The generic layer does
not perform provider writes or interpret provider payloads.

## Linear Issue Webhooks

The built-in Linear webhook adapter maps each authenticated `Comment` or `Issue`
event to `linear:<organization-id>:issue` plus the immutable Linear issue ID.
The webhook receipt retains the delivery UUID, while the conversation delivery
stores only the host-parsed prompt, readable control title, and closed event
metadata needed to wake the agent. Raw provider shapes do not cross the routing
boundary.

An issue update that requests planning, waits for plan approval, or acquires or
confirms an active execution lease is the exception: the Linear integration
records it as controller-owned instead of creating a conversation delivery. A
Temporal-reconciled isolated task owns planning or execution, so the generic
issue conversation cannot start a second agent against the same work. Comment
events remain ordered conversation input.

Webhook admission persists the receipt before linking the FIFO delivery. An
exact provider replay always attempts the link again, which repairs a crash
between those writes. Duplicate delivery IDs reuse the existing FIFO entry.
Separate issue subjects can hold claims concurrently; deliveries for one issue
wait behind its claimed FIFO head.

The adapter places the stable routed workspace behind a Discord child thread of
the configured Linear workspace. A first callback derives a title such as
`[PYN-123] Repair scheduler`. Later callbacks retain the persisted title and
binding. If Discord no longer has that thread, reconciliation creates a
replacement, moves the runtime workspace to the replacement JID, and rebinds
the conversation's existing agent session.

Before admission, the host uses the API key from the route's named Linear account
to fetch the current issue and rejects a delivery that does not belong to the
route's workspace Project. The same account declaration controls prompt handling.
A private source sends the parsed comment as trusted conversation input; a public
source fences the same context and starts the invocation corruption-tainted. Both
paths preserve a short, provider-owned wake-up prompt instead of asking the model
to perform board membership checks. Explicit Linear lifecycle actions still
enforce planning and execution workflow state.

The adapter maps the current Linear `Done` state to closed control intent and any
other named issue state to open intent. Events without issue state, such as
minimal Comment payloads, preserve the binding's current intent. This keeps
Linear authoritative without deriving lifecycle from mutable Discord state.

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
