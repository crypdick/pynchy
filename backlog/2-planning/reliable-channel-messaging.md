# Reliable Bidirectional Channel Messaging

## Problem

The current messaging architecture conflates three concerns into two shared cursors:

1. **`last_timestamp`** (global): "what messages has the polling loop seen?" — shared across all channels and groups.
2. **`last_agent_timestamp[group_jid]`** (per-group): "what messages has each group's agent processed?" — but also reused as the catch-up cutoff for channel API history fetches.

These two cursors try to answer three distinct questions:
- What has each channel **sent us** (inbound)?
- What has each channel **received from us** (outbound)?
- What has the **agent processed**?

### Specific Failures

**Non-atomic cursor persistence** — `_save_state()` (`app.py:109-115`) makes two separate `INSERT OR REPLACE` + `commit()` calls. A crash between them leaves cursors inconsistent.

**Outbound fire-and-forget** — `bus.broadcast()` (`bus.py:77`) catches errors and logs but never retries. A transient network blip means a channel permanently misses an outbound message.

**Only Slack has catch-up** — WhatsApp has no `catch_up()` method. If neonize drops an event, it's gone until someone notices.

**Catch-up anchored to agent cursor** — `_catch_up_channel_history()` (`app.py:414`) passes `last_agent_timestamp` to `ch.catch_up()`. If the agent cursor rolls back on error, catch-up re-fetches already-ingested messages.

**Channel-agnostic cursor namespace** — `last_agent_timestamp` is keyed by canonical JID with no concept of per-channel cursors.

**Duck-typed catch-up** — `hasattr(ch, "catch_up")` checks at call sites. No uniform interface for plugin authors.

## Design

### Per-Channel Bidirectional Cursors

Replace the two shared cursors with **per-channel, per-group, bidirectional cursors** in a new `channel_cursors` table. Each cursor tracks one direction of communication between one channel and one group.

```sql
CREATE TABLE IF NOT EXISTS channel_cursors (
    channel_name  TEXT NOT NULL,   -- 'slack', 'whatsapp', 'tui', ...
    chat_jid      TEXT NOT NULL,   -- canonical group JID
    direction     TEXT NOT NULL,   -- 'inbound' or 'outbound'
    cursor_value  TEXT NOT NULL,   -- ISO timestamp (or channel-native token)
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (channel_name, chat_jid, direction)
);
```

| Cursor | Meaning | Who advances it |
|--------|---------|----------------|
| `(slack, group_X, inbound)` | Latest message ingested from Slack for group X | Reconciler after ingesting |
| `(slack, group_X, outbound)` | Latest message confirmed delivered to Slack for group X | `bus.broadcast()` on success |
| `(whatsapp, group_X, inbound)` | Latest message ingested from WhatsApp for group X | Reconciler after ingesting |
| `(whatsapp, group_X, outbound)` | Latest message confirmed delivered to WhatsApp for group X | `bus.broadcast()` on success |

The existing `last_agent_timestamp[group_jid]` and global `last_timestamp` stay — they track agent processing and polling-loop state respectively, which are orthogonal to channel cursors.

### Standardized Channel Protocol

Every channel implements reconciliation methods as part of the `Channel` protocol — no more duck-typed `hasattr` checks. Channels where a method doesn't apply implement it as a no-op:

```python
class Channel(Protocol):
    # ... existing required methods (connect, send_message, is_connected, owns_jid, disconnect) ...

    async def fetch_inbound_since(
        self, channel_jid: str, since: str
    ) -> list[NewMessage]:
        """Fetch messages from channel API newer than `since`.

        Channels without server-side history (e.g. TUI) return [].
        """
        ...

    async def confirm_outbound(
        self, channel_jid: str, message_id: str
    ) -> bool:
        """Check if an outbound message was successfully delivered.

        Channels that can't verify delivery return True (optimistic).
        """
        ...
```

| Channel | `fetch_inbound_since` | `confirm_outbound` |
|---------|----------------------|-------------------|
| **Slack** | `conversations.history` (existing logic refactored) | `conversations.history` check |
| **WhatsApp** | neonize history API if available, else `return []` | `return True` (no server-side check) |
| **TUI** | `return []` (reads DB directly via SSE) | `return True` (local delivery) |

JID resolution moves **out of** the channel into the reconciler. Channels receive a resolved channel-native JID and don't need to understand the alias system.

### Unified Reconciliation Loop

Replaces `_catch_up_channel_history()`. Single code path for all channels:

```python
async def reconcile_all_channels(app) -> None:
    """Single reconciliation pass across all channels and groups."""
    for ch in app.channels:
        for canonical_jid in app.workspaces:
            channel_jid = resolve_channel_jid(app, canonical_jid, ch.name)
            if not channel_jid:
                continue

            # --- Inbound ---
            inbound_cursor = await get_channel_cursor(ch.name, canonical_jid, 'inbound')
            remote_messages = await ch.fetch_inbound_since(channel_jid, inbound_cursor)
            new_inbound_cursor = inbound_cursor
            for msg in remote_messages:
                if not await message_exists(msg.id, canonical_jid):
                    await ingest_user_message(app, msg, source_channel=ch.name)
                    app.queue.enqueue_message_check(canonical_jid)
                new_inbound_cursor = max(new_inbound_cursor, msg.timestamp)

            # --- Outbound ---
            outbound_cursor = await get_channel_cursor(ch.name, canonical_jid, 'outbound')
            pending_sends = await get_pending_outbound(ch.name, canonical_jid, outbound_cursor)
            new_outbound_cursor = outbound_cursor
            for pending in pending_sends:
                try:
                    await ch.send_message(channel_jid, pending.content)
                    new_outbound_cursor = max(new_outbound_cursor, pending.timestamp)
                except Exception:
                    break  # preserve ordering

            # --- Atomic cursor update ---
            await advance_cursors_atomic(
                ch.name, canonical_jid,
                inbound=new_inbound_cursor,
                outbound=new_outbound_cursor,
            )
```

### Outbound Ledger

Track what needs to be delivered per channel:

```sql
CREATE TABLE IF NOT EXISTS outbound_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_jid      TEXT NOT NULL,
    content       TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    source        TEXT NOT NULL,     -- 'agent', 'host', 'cross_post'
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);

CREATE TABLE IF NOT EXISTS outbound_deliveries (
    ledger_id     INTEGER NOT NULL,
    channel_name  TEXT NOT NULL,
    delivered_at  TEXT,              -- NULL = pending
    error         TEXT,
    PRIMARY KEY (ledger_id, channel_name),
    FOREIGN KEY (ledger_id) REFERENCES outbound_ledger(id)
);
```

`bus.broadcast()` changes:
1. Write to `outbound_ledger`
2. For each channel, attempt delivery → write to `outbound_deliveries`
3. On success: set `delivered_at`
4. On failure: set `error`, leave `delivered_at` NULL
5. Reconciler retries NULL `delivered_at` rows

### Atomic State Persistence

Wrap `_save_state()` in a single transaction:

```python
async def _save_state(self) -> None:
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute("BEGIN")
    try:
        await db.execute(
            "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
            ("last_timestamp", self.last_timestamp),
        )
        await db.execute(
            "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
            ("last_agent_timestamp", json.dumps(self.last_agent_timestamp)),
        )
        await db.commit()
    except:
        await db.rollback()
        raise
```

Same pattern for `advance_cursors_atomic()` — all cursor advances for a processing cycle in one transaction.

## Implementation Phases

### Phase 1: Foundation (non-breaking)
- Add `channel_cursors`, `outbound_ledger`, `outbound_deliveries` tables via migration
- Add DB functions: `get_channel_cursor()`, `set_channel_cursor()`, `advance_cursors_atomic()`, `record_outbound()`, `get_pending_outbound()`
- Add `fetch_inbound_since()` and `confirm_outbound()` to `Channel` protocol in `types.py`
- Implement no-op versions in TUI channel

### Phase 2: Inbound reconciliation
- Refactor Slack `catch_up()` → `fetch_inbound_since()` (move JID resolution out)
- Add `fetch_inbound_since()` to WhatsApp (no-op or neonize history)
- Replace `_catch_up_channel_history()` with unified `reconcile_all_channels()`
- Seed `channel_cursors` from `last_agent_timestamp` on first boot (migration)
- Run old and new paths in parallel during validation

### Phase 3: Outbound ledger + retry
- Modify `bus.broadcast()` to write to `outbound_ledger`
- Track per-channel delivery in `outbound_deliveries`
- Add outbound reconciliation to the reconciliation loop
- Implement `confirm_outbound()` on Slack
- GC old ledger entries (>24h with all channels delivered)

### Phase 4: Atomic state persistence
- Wrap `_save_state()` in single transaction
- Ensure all cursor advances happen atomically
- Enable WAL mode if not already active

## What Stays the Same

- `last_timestamp` (global polling cursor)
- `last_agent_timestamp[group_jid]` (per-group agent cursor)
- `GroupQueue` (serialization and concurrency)
- `ingest_user_message()` (unified ingestion pipeline)
- IPC (container communication)
- Core `Channel` protocol methods (connect, send_message, disconnect, etc.)

## What Changes

| Before | After |
|--------|-------|
| `ch.catch_up(canonical_to_aliases, last_agent_timestamp)` | `ch.fetch_inbound_since(channel_jid, inbound_cursor)` |
| Catch-up anchored to agent cursor | Catch-up anchored to per-channel inbound cursor |
| `hasattr(ch, "catch_up")` duck typing | Uniform `Channel` protocol — all channels implement |
| `broadcast()` fire-and-forget | `broadcast()` writes ledger, reconciler retries |
| `_save_state()` two separate commits | Single atomic transaction |
| Only Slack has catch-up | All channels participate via shared code path |
| Channels resolve JID aliases internally | Reconciler resolves JIDs, channels receive native JID |

## Risk Mitigation

- Phase 1 is fully backwards-compatible (new tables, nothing reads them)
- Phase 2 keeps old cursors running in parallel — remove old path after validation
- Phase 3 falls back to fire-and-forget if ledger write fails
- Migration seeds new cursors from existing state — no message re-fetch on deploy
