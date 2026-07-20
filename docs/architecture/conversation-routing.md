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

Startup releases process-owned claims back to `pending`. Sequence numbers remain
unchanged, so the interrupted FIFO head gets claimed again before later
deliveries. Provider integrations complete or release the claim after their
agent turn; an idle conversation carries no claim.

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

## Integration Boundary

This layer provides persistence, typed identities, claims, sessions, and control
reconciliation. Provider plugins still decide how an authenticated event maps to
an immutable subject and when to enqueue agent execution. The generic layer does
not perform provider writes or interpret provider payloads.

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
room ID or redirect a send.

The control thread is also the approval surface. Every Matrix send requires
human approval in that thread, bound to the exact connection, route,
conversation, control thread, room, portal assertion, and body. Replay rebuilds
the current workspace policy and rechecks the live portal before transmitting.
See [Matrix communications](../integrations/matrix-gateway.md) for configuration.

For ordinary channel messages and interrupted agent turns, see
[Message routing](message-routing.md). For workspace inheritance below Discord
parents, see [Workspaces](workspaces.md).
