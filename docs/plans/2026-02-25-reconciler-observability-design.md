# Reconciler Observability — Design

**Date:** 2026-02-25
**Context:** A Slack message was silently dropped during a crash cycle and the reconciler failed to recover it. Investigation found zero evidence the reconciler has ever recovered a message in production, and all skip paths are silent.

## Problem

The reconciler (`chat/reconciler.py`) has three code paths that silently skip recovery:

1. **Connection name gate** (line 70-71) — skips (channel, workspace) pairs where the workspace's configured connection doesn't match the channel being iterated.
2. **JID ownership check** (line 73-75) — skips pairs where neither `get_channel_jid()` finds an alias nor `ch.owns_jid()` matches.
3. **Fetch exception** (line 96-102) — catches all exceptions from `fetch_inbound_since()`, logs a generic warning without the error, and continues.

Additionally, stale cursors from a previous channel name (`slack` → `connection.slack.synapse`) accumulate in the `channel_cursors` table and are never cleaned up.

## Design

### 1. Debug logging on skip paths

Add structured `logger.debug()` calls on all three skip paths so the next silent drop can be diagnosed from journalctl.

- **Connection gate skip:** log `channel`, `canonical_jid`, `expected_connection`, `actual_channel`
- **JID ownership skip:** log `channel`, `canonical_jid`, `channel_jid_result`, `owns_jid_result`
- **Fetch exception:** include `exc_info=True` or `error=str(exc)` in the existing warning

### 2. Stale cursor garbage collection

At the end of `reconcile_all_channels()`, after the main loop, collect the set of active channel names from `deps.channels` and prune any `channel_cursors` rows whose `channel_name` is not in that set.

- Runs on every reconciliation cycle (cheap — single DELETE query)
- Prevents unbounded accumulation of dead cursors after channel renames
- Log pruned count at info level when > 0

### 3. Recovery summary log

Change the existing `recovered`/`retried` log from info to always emit, even when counts are zero, at debug level. This confirms the reconciler actually ran through the full loop (vs. being short-circuited). Keep info-level log when counts > 0.

## Files

| File | Change |
|------|--------|
| `src/pynchy/chat/reconciler.py` | Debug logs on skip paths, cursor GC, recovery summary |
| `src/pynchy/db/cursors.py` (or equivalent) | Add `prune_stale_cursors(active_channel_names)` query |

## Non-goals

- Changing the reconciler's skip logic itself (the connection gate and JID ownership checks are correct by design)
- User-facing notifications (the :eyes: reaction from the normal pipeline is sufficient)
- Root-causing the specific incident (requires logs we don't have; the logging fix prevents this blind spot going forward)
