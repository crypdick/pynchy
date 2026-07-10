# Phoenix Conversation Store Design

## Context

Pynchy currently splits conversation durability across two stores:

- SQLite stores the local chat ledger: inbound channel messages, final assistant
  replies, host messages, command outputs, cursors, clear boundaries, and
  dashboard history.
- LiteLLM exports model request/response traces to Phoenix. Phoenix is already
  the source of truth for prompts, responses, tool-call traces, token usage,
  cost, and provider request metadata.

That split makes SQLite more important than it should be. SQLite should stay a
local index and queueing substrate, but durable conversation content should live
in Phoenix.

## Scope

This design moves new conversation content to Phoenix as the durable store and
keeps SQLite as a lightweight projection.

It includes:

- a canonical host-side conversation event model;
- host-side Phoenix writes for events LiteLLM cannot see;
- deterministic correlation between host events and LiteLLM spans;
- SQLite pointer rows for local polling, cursors, status, and fast history
  lookup;
- read paths that hydrate content from Phoenix when a body is required.

It does not include:

- backfilling historical SQLite messages into Phoenix;
- local failover or delayed replay when Phoenix is unavailable;
- replacing LiteLLM's direct Phoenix callback for model spans;
- direct application access to Phoenix's backing Postgres database.

## Approaches Considered

### Recommended: Phoenix content store with SQLite projection

Pynchy writes every new host-visible conversation event to Phoenix first. After
Phoenix accepts the event, Pynchy inserts a small SQLite projection row with the
event id, chat id, timestamp, type, and Phoenix pointer. LiteLLM continues to
write model spans directly to Phoenix, using the same turn and event ids that
Pynchy generates before a turn starts.

This keeps Phoenix as the durable content store while preserving SQLite's local
strengths: fast polling, cursor management, restart recovery, and dashboard
listing.

### Rejected: Phoenix observer plugin as a parallel trace path

A host-side observer that subscribes to `AgentTraceEvent` can duplicate some
trace data into Phoenix, but it does not define the canonical event model or
replace SQLite as the chat content store. It also risks creating two competing
Phoenix histories: one from LiteLLM model spans and one from host observer spans.

### Rejected: Direct Postgres integration

Phoenix's Postgres database is an implementation detail. Pynchy should speak to
Phoenix through OTLP or Phoenix-supported APIs, not by writing rows into Phoenix's
database. Direct database writes couple Pynchy to Phoenix internals and bypass
Phoenix's ingestion semantics.

### Rejected: SQLite content with Phoenix pointers

Keeping full bodies in SQLite and only attaching Phoenix trace ids preserves the
current split. It does not make Phoenix the durable source of truth for
conversation content.

## Event Model

Pynchy owns a host-side `ConversationEvent` model for events outside LiteLLM's
visibility.

Required fields:

- `event_id`: stable UUID or generated id, unique across Pynchy.
- `turn_id`: stable id for one agent turn, or `null` for events outside a turn.
- `chat_jid`: physical chat id.
- `workspace_folder`: Pynchy workspace folder.
- `timestamp`: UTC ISO timestamp.
- `kind`: one of `user_message`, `assistant_message`, `host_message`,
  `command_output`, `security_audit`, `clear_chat`, `turn_start`, `turn_end`,
  `channel_delivery`, or `system_notice`.
- `sender`: logical sender vocabulary, such as `slack:C123`, `bot`, `host`,
  `command_output`, or `security`.
- `sender_name`: display sender name when available.
- `content`: event body for content-bearing events.
- `metadata`: structured details that should not be flattened into text.

Event ids are generated before persistence. Pynchy writes the same ids into
Phoenix attributes and SQLite projection rows, so correlation never depends on
string matching or timestamp proximity.

## Write Path

Every new content-bearing event follows this order:

1. Build a `ConversationEvent`.
2. Write the full event to Phoenix.
3. Insert or update the SQLite projection row.
4. Emit the local EventBus event.
5. Advance local cursors or enqueue agent processing.

If Phoenix write fails, Pynchy fails the operation and does not write a SQLite
pointer. Without failover, this is intentional: an event that has not reached
Phoenix has not entered durable conversation history.

## LiteLLM Correlation

LiteLLM remains the direct Phoenix writer for model spans.

Before starting an agent turn, Pynchy creates:

- a `turn_id`;
- a `turn_start` conversation event;
- one `user_message` event per inbound user message included in the turn.

Pynchy passes `turn_id`, `chat_jid`, `workspace_folder`, and relevant
`event_id` values into the LiteLLM request metadata. LiteLLM's Phoenix callback
then writes model spans with the same attributes. Phoenix queries can reconstruct
one turn by selecting host conversation events and LiteLLM spans with the same
`turn_id`.

The final assistant content has two representations:

- LiteLLM stores the model response body in the model span.
- Pynchy stores an `assistant_message` event for the channel-visible result,
  including host-tag formatting, stream finalization, channel delivery ids, and
  any post-processing that happens after the raw model response.

Those events share the same `turn_id` and can reference the model span id once
available.

## SQLite Projection

SQLite keeps only local indexing and operational fields. A new projection table
replaces full message bodies for new rows.

Conceptual columns:

- `event_id`
- `chat_jid`
- `turn_id`
- `timestamp`
- `kind`
- `sender`
- `sender_name`
- `is_from_me`
- `message_type`
- `phoenix_trace_id`
- `phoenix_span_id`
- `phoenix_event_ref`
- `preview`
- `metadata`

`preview` is bounded text for dashboards and logs. It is not the durable body.
`metadata` stores small local fields needed for routing or UI, not duplicated
content payloads.

Existing SQLite tables can remain during migration, but new write APIs should
target the projection. Compatibility readers can return `NewMessage` by hydrating
body content from Phoenix and filling the existing dataclass shape.

## Read Path

Local workflows query SQLite for event ids and ordering, then hydrate bodies from
Phoenix when content is needed.

Examples:

- Agent turn assembly calls `get_messages_since`, receives pointer rows, fetches
  bodies from Phoenix, and builds the LLM input.
- `/api/messages` reads recent pointers and returns previews immediately or full
  hydrated content when requested.
- Clear/reset stores a `clear_chat` event in Phoenix and updates the SQLite clear
  boundary locally.

Phoenix read failure is a hard error for content hydration. Pynchy should report
that the durable history store is unavailable instead of falling back to stale or
partial SQLite bodies.

## Failure Behavior

Phoenix is required for conversation content writes.

- If Phoenix is unavailable when receiving a user message, Pynchy does not store
  the message locally and does not run the agent.
- If Phoenix is unavailable when storing an assistant result, Pynchy treats the
  turn as failed before advancing durable local state.
- If SQLite projection write fails after a Phoenix write succeeds, Pynchy can
  retry the projection because the durable event id already exists in Phoenix.
  Projection retry is not a content failover path.

This design deliberately skips a local spool. Operators should fix Phoenix rather
than reconcile divergent local history.

## Migration

The first implementation is a forward-only cutover.

- Historical SQLite chat rows remain readable through the current path.
- New messages use Phoenix-first writes.
- No backfill job imports old SQLite content into Phoenix.
- After the cutover proves stable, SQLite content retention can shrink or stop
  writing `messages.content` for new rows.

The code should make mixed history explicit: old rows have local bodies; new rows
have Phoenix pointers.

## Security And Privacy

Moving content to Phoenix centralizes sensitive conversation data in one service.
Pynchy should treat Phoenix availability and access as part of the production
trust boundary.

Host-side Phoenix writes must use the same network and auth posture as LiteLLM's
Phoenix callback. Pynchy must not log full event bodies when Phoenix writes fail.
SQLite previews should be bounded and avoid storing large tool outputs or full
prompts.

## Testing

Unit tests should cover:

- event id and turn id generation;
- Phoenix write before SQLite projection insert;
- no SQLite insert when Phoenix write fails;
- projection retry behavior when SQLite fails after Phoenix succeeds;
- LiteLLM metadata includes `turn_id`, `chat_jid`, and event ids;
- `get_messages_since` hydrates Phoenix-backed rows;
- mixed old-SQLite and new-Phoenix history reads;
- clear-chat behavior with Phoenix event plus SQLite boundary;
- `/api/messages` returning previews without full hydration.

Integration tests should use a fake Phoenix OTLP/API server. The default test
suite must not require a real Phoenix deployment.

## Follow-Up Work

After the forward-only cutover:

- retire old full-body SQLite writes for new rows;
- add a dashboard affordance that links pointer rows to Phoenix spans;
- define retention for SQLite previews and projection rows;
- consider whether security audit rows should keep local redacted copies or move
  fully to Phoenix pointers.
