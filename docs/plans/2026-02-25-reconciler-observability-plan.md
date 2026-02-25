# Reconciler Observability — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add debug logging to the reconciler's silent skip paths and garbage-collect stale cursors after channel renames.

**Architecture:** Three small changes to two files: (1) debug logs on the three silent skip paths in the reconciler, (2) a `prune_stale_cursors()` DB function, (3) call it from the reconciler with a summary log.

**Tech Stack:** Python, aiosqlite, structlog, pytest

---

### Task 1: Add `prune_stale_cursors()` DB function

**Files:**
- Modify: `src/pynchy/db/channel_cursors.py` (append new function)
- Modify: `src/pynchy/db/__init__.py` (re-export)
- Test: `tests/test_channel_cursors.py`

**Step 1: Write the failing test**

Add to the end of `tests/test_channel_cursors.py`:

```python
@pytest.mark.usefixtures("_db")
class TestPruneStaleCursors:
    @pytest.mark.asyncio
    async def test_deletes_cursors_for_unknown_channels(self):
        await set_channel_cursor("old-channel", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("active-channel", "group@g.us", "inbound", "2024-06-01")

        pruned = await prune_stale_cursors({"active-channel"})

        assert pruned == 1
        assert await get_channel_cursor("old-channel", "group@g.us", "inbound") == ""
        assert await get_channel_cursor("active-channel", "group@g.us", "inbound") == "2024-06-01"

    @pytest.mark.asyncio
    async def test_noop_when_all_channels_active(self):
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01")

        pruned = await prune_stale_cursors({"slack"})

        assert pruned == 0
        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-01-01"

    @pytest.mark.asyncio
    async def test_noop_on_empty_table(self):
        pruned = await prune_stale_cursors({"slack"})
        assert pruned == 0
```

Also add `prune_stale_cursors` to the import at the top of the test file.

**Step 2: Run test to verify it fails**

Run: `uvx pytest tests/test_channel_cursors.py::TestPruneStaleCursors -v`
Expected: FAIL with `ImportError` (function doesn't exist yet)

**Step 3: Implement `prune_stale_cursors`**

Append to `src/pynchy/db/channel_cursors.py`:

```python
async def prune_stale_cursors(active_channel_names: set[str]) -> int:
    """Delete cursors for channels no longer in the active set.

    Returns the number of rows deleted.
    """
    if not active_channel_names:
        return 0
    db = _get_db()
    placeholders = ",".join("?" for _ in active_channel_names)
    cursor = await db.execute(
        f"DELETE FROM channel_cursors WHERE channel_name NOT IN ({placeholders})",
        tuple(active_channel_names),
    )
    await db.commit()
    return cursor.rowcount
```

Add to `src/pynchy/db/__init__.py`:
- Import: add `prune_stale_cursors` to the `from pynchy.db.channel_cursors import (...)` block
- `__all__`: add `"prune_stale_cursors"` to the `# channel_cursors` section

**Step 4: Run test to verify it passes**

Run: `uvx pytest tests/test_channel_cursors.py::TestPruneStaleCursors -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/pynchy/db/channel_cursors.py src/pynchy/db/__init__.py tests/test_channel_cursors.py
git commit -m "feat(db): add prune_stale_cursors for cursor GC after channel renames"
```

---

### Task 2: Add debug logging to reconciler skip paths

**Files:**
- Modify: `src/pynchy/chat/reconciler.py` (lines 69-75, 96-102, 139-142)
- Test: `tests/test_reconciler.py`

**Step 1: Write the failing tests**

Add to the end of `tests/test_reconciler.py`:

```python
# ---------------------------------------------------------------------------
# Debug logging on skip paths
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestSkipPathLogging:
    @pytest.mark.asyncio
    async def test_logs_connection_gate_skip(self, caplog):
        """When connection name doesn't match, a debug log is emitted."""
        ch = _make_channel(name="whatsapp")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with caplog.at_level("DEBUG", logger="pynchy.chat.reconciler"):
            await reconcile_all_channels(deps)

        assert any("connection_gate_skip" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_logs_jid_ownership_skip(self, caplog):
        """When neither alias nor ownership matches, a debug log is emitted."""
        ch = _make_channel(owns=False)
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with caplog.at_level("DEBUG", logger="pynchy.chat.reconciler"):
            await reconcile_all_channels(deps)

        assert any("jid_ownership_skip" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_logs_fetch_exception_with_error(self, caplog):
        """When fetch_inbound_since raises, the error detail is logged."""
        ch = _make_channel()
        ch.fetch_inbound_since = AsyncMock(side_effect=RuntimeError("socket closed"))
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")

        with caplog.at_level("WARNING", logger="pynchy.chat.reconciler"):
            await reconcile_all_channels(deps)

        assert any("socket closed" in r.message for r in caplog.records)
```

Note: the `AsyncMock` import is already at the top of the test file.

**Step 2: Run tests to verify they fail**

Run: `uvx pytest tests/test_reconciler.py::TestSkipPathLogging -v`
Expected: FAIL — no matching log records (current code has no debug logs on these paths)

**Step 3: Add debug logging to `reconciler.py`**

In `src/pynchy/chat/reconciler.py`, make these changes:

**Connection gate skip** — replace lines 69-71:
```python
# Before:
            if group is not None:
                expected = resolve_workspace_connection_name(group.folder)
                if expected and expected != ch.name:
                    continue

# After:
            if group is not None:
                expected = resolve_workspace_connection_name(group.folder)
                if expected and expected != ch.name:
                    logger.debug(
                        "connection_gate_skip",
                        channel=ch.name,
                        canonical_jid=canonical_jid,
                        expected=expected,
                    )
                    continue
```

**JID ownership skip** — replace lines 73-75:
```python
# Before:
            channel_jid = deps.get_channel_jid(canonical_jid, ch.name)
            if not channel_jid and not ch.owns_jid(canonical_jid):
                continue

# After:
            channel_jid = deps.get_channel_jid(canonical_jid, ch.name)
            if not channel_jid and not ch.owns_jid(canonical_jid):
                logger.debug(
                    "jid_ownership_skip",
                    channel=ch.name,
                    canonical_jid=canonical_jid,
                )
                continue
```

**Fetch exception** — replace lines 96-102:
```python
# Before:
            except Exception:
                logger.warning(
                    "fetch_inbound_since failed",
                    channel=ch.name,
                    jid=canonical_jid,
                )
                continue

# After:
            except Exception as exc:
                logger.warning(
                    "fetch_inbound_since failed",
                    channel=ch.name,
                    jid=canonical_jid,
                    error=str(exc),
                )
                continue
```

**Step 4: Run tests to verify they pass**

Run: `uvx pytest tests/test_reconciler.py::TestSkipPathLogging -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/pynchy/chat/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): add debug logging to silent skip paths"
```

---

### Task 3: Wire cursor GC and summary log into reconciler

**Files:**
- Modify: `src/pynchy/chat/reconciler.py` (after main loop, ~line 137)
- Test: `tests/test_reconciler.py`

**Step 1: Write the failing tests**

Add to the end of `tests/test_reconciler.py`:

```python
# ---------------------------------------------------------------------------
# Cursor GC
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestCursorGC:
    @pytest.mark.asyncio
    async def test_prunes_stale_cursors_after_reconciliation(self):
        """Cursors for channels not in deps.channels are pruned."""
        await set_channel_cursor("dead-channel", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-06-01")

        ch = _make_channel(name="slack")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)

        assert await get_channel_cursor("dead-channel", "group@g.us", "inbound") == ""
        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-06-01"

    @pytest.mark.asyncio
    async def test_logs_pruned_count(self, caplog):
        await set_channel_cursor("dead-channel", "group@g.us", "inbound", "2024-01-01")

        ch = _make_channel(name="slack")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        with caplog.at_level("INFO", logger="pynchy.chat.reconciler"):
            await reconcile_all_channels(deps)

        assert any("Pruned stale cursors" in r.message for r in caplog.records)
```

**Step 2: Run tests to verify they fail**

Run: `uvx pytest tests/test_reconciler.py::TestCursorGC -v`
Expected: FAIL — stale cursor still exists after reconciliation

**Step 3: Add cursor GC and summary log to reconciler**

In `src/pynchy/chat/reconciler.py`:

Add import at top (alongside existing `from pynchy.db import ...`):
```python
from pynchy.db import (
    advance_cursors_atomic,
    get_channel_cursor,
    get_pending_outbound,
    mark_delivered,
    mark_delivery_error,
    message_exists,
    prune_stale_cursors,
)
```

Replace the summary log block at the end of `reconcile_all_channels()` (lines 139-142):
```python
# Before:
    if recovered:
        logger.info("Recovered missed channel messages", count=recovered)
    if retried:
        logger.info("Retried pending outbound deliveries", count=retried)

# After:
    if recovered:
        logger.info("Recovered missed channel messages", count=recovered)
    if retried:
        logger.info("Retried pending outbound deliveries", count=retried)
    if not recovered and not retried:
        logger.debug("Reconciliation complete, nothing to recover")

    # GC cursors for channels that no longer exist (e.g. after a rename)
    active_names = {ch.name for ch in deps.channels}
    pruned = await prune_stale_cursors(active_names)
    if pruned:
        logger.info("Pruned stale cursors", count=pruned)
```

**Step 4: Run tests to verify they pass**

Run: `uvx pytest tests/test_reconciler.py::TestCursorGC -v`
Expected: PASS (2 tests)

**Step 5: Run full test suite for both files**

Run: `uvx pytest tests/test_reconciler.py tests/test_channel_cursors.py -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add src/pynchy/chat/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): wire cursor GC and recovery summary log"
```
