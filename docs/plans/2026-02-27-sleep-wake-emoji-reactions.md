# Sleep/Wake Emoji Reactions — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `:sunrise:` reaction when a sleeping workspace receives a message, and `:zzz:` reaction on the agent's final response when it finishes.

**Architecture:** Two independent features sharing the reaction infrastructure. Feature 1 (sunrise) adds a single `send_reaction_to_channels()` call in the inbound routing "no active container" branch. Feature 2 (zzz) stashes per-channel outbound message IDs from the streaming pipeline, then consumes them in the message processing pipeline to send the reaction after the agent finishes.

**Tech Stack:** Python async, existing `send_reaction_to_channels` infrastructure, Slack reactions API

**Design doc:** `docs/plans/2026-02-27-sleep-wake-emoji-reactions-design.md`

---

### Task 1: Sunrise reaction — test

**Files:**
- Test: `tests/test_message_handler.py`

The existing test `test_system_notice_with_user_message_wakes_agent` (line 1319) verifies that `enqueue_message_check` is called when a real message wakes the agent. We need a companion test that also verifies the sunrise reaction is sent.

**Step 1: Write the failing test**

Add to the `TestRouteIncomingGroup` class (or whichever class contains the wake tests — look for `test_system_notice_with_user_message_wakes_agent` at line 1319):

```python
@pytest.mark.asyncio
async def test_sunrise_reaction_on_wake(self):
    """When a message wakes a sleeping workspace, the first message
    in the batch should get a :sunrise: reaction."""
    jid = "group@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(
        groups={jid: group},
        last_agent_ts={jid: "old-ts"},
    )
    deps.queue.is_active_task.return_value = False
    deps.queue.send_message.return_value = False

    msg1 = _make_message("hello", id="msg-1", timestamp="ts-1")
    msg2 = _make_message("world", id="msg-2", timestamp="ts-2")

    with (
        patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
        patch(
            _PR_NEW_MSGS,
            new_callable=AsyncMock,
            return_value=([msg1, msg2], "poll-ts"),
        ),
        patch(
            _PR_MSGS_SINCE,
            new_callable=AsyncMock,
            return_value=[msg1, msg2],
        ),
        patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
    ):
        await _run_loop_once(deps)

    # Only the first message gets sunrise
    deps.send_reaction_to_channels.assert_awaited_once_with(
        jid, "msg-1", msg1.sender, "sunrise"
    )
    # Still enqueues the run
    deps.queue.enqueue_message_check.assert_called_once_with(jid)
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_message_handler.py::TestRouteIncomingGroup::test_sunrise_reaction_on_wake -xvs 2>&1 | tail -20`

Note: Find the correct class name by looking for where `test_system_notice_with_user_message_wakes_agent` lives — use that same class.

Expected: FAIL — `send_reaction_to_channels` is not called (currently no sunrise reaction in the code).

---

### Task 2: Sunrise reaction — implement

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/inbound.py:153-155`

**Step 3: Add the sunrise reaction**

In `_route_incoming_group()`, at the "no active container" branch (line 153-155), add the reaction before `enqueue_message_check`. The current code is:

```python
    # --- No active container: enqueue a new run ---
    logger.info("route_trace", step="enqueue_new_run", group=group.name)
    deps.queue.enqueue_message_check(group_jid)
```

Change to:

```python
    # --- No active container: enqueue a new run ---
    logger.info("route_trace", step="enqueue_new_run", group=group.name)
    first_msg = group_messages[0]
    await deps.send_reaction_to_channels(group_jid, first_msg.id, first_msg.sender, "sunrise")
    deps.queue.enqueue_message_check(group_jid)
```

**Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_message_handler.py::TestRouteIncomingGroup::test_sunrise_reaction_on_wake -xvs 2>&1 | tail -20`

Expected: PASS

**Step 5: Run full test suite for inbound routing**

Run: `uv run python -m pytest tests/test_message_handler.py -x 2>&1 | tail -10`

Expected: All pass. Check that existing tests still pass — the mock's `send_reaction_to_channels` is already an `AsyncMock`, so existing tests that don't assert on it will be unaffected.

**Step 6: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/inbound.py tests/test_message_handler.py
git commit -m "feat: add sunrise reaction when message wakes sleeping workspace"
```

---

### Task 3: Zzz reaction — stash outbound IDs in router

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/router.py:50` (module-level), `router.py:339-403` (`_handle_final_result`)
- Test: `tests/test_messaging_router.py`

The streaming pipeline tracks per-channel message IDs in `stream_state.message_ids` (`{channel_name: raw_slack_ts}`). We need to stash these after the final result is sent so the processing pipeline can retrieve them for the zzz reaction.

**Step 7: Write the failing test**

Add to `tests/test_messaging_router.py` in the `TestHandleStreamedOutput` class:

```python
@pytest.mark.asyncio
async def test_final_result_stashes_outbound_ids(self):
    """When a result with text is handled, the per-channel message IDs
    from the stream state should be stashed in _last_result_ids."""
    from pynchy.host.orchestrator.messaging.router import _last_result_ids

    deps = _make_deps()
    group = _make_group()
    output = _make_output(type="result", result="Hello!")
    chat_jid = "g@g.us"

    # Pre-populate stream state with a fake channel message ID
    stream_states[chat_jid] = StreamState(
        buffer="Hello!",
        message_ids={"test": "1234567890.000001"},
    )

    with patch(
        "pynchy.host.orchestrator.messaging.router.store_message_direct",
        new_callable=AsyncMock,
    ):
        result = await handle_streamed_output(deps, chat_jid, group, output)

    assert result is True
    assert _last_result_ids.get(chat_jid) == {"test": "1234567890.000001"}

    # Clean up
    _last_result_ids.pop(chat_jid, None)
```

**Step 8: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_messaging_router.py::TestHandleStreamedOutput::test_final_result_stashes_outbound_ids -xvs 2>&1 | tail -20`

Expected: FAIL — `_last_result_ids` doesn't exist yet.

**Step 9: Implement the stash**

In `router.py`, add the module-level dict after `_trace_counter` (around line 50):

```python
# Per-chat outbound message IDs from the last final result.
# Populated by _handle_final_result(), consumed by pop_last_result_ids().
# Keyed by chat_jid → {channel_name: raw_message_ts}.
_last_result_ids: dict[str, dict[str, str]] = {}
```

In `_handle_final_result()`, after `finalize_stream_or_broadcast` (line 391-393), add:

```python
    # Stash per-channel message IDs for post-run reactions (e.g. zzz).
    if stream_ids:
        _last_result_ids[chat_jid] = dict(stream_ids)
```

Add a public function to consume the IDs:

```python
def pop_last_result_ids(chat_jid: str) -> dict[str, str] | None:
    """Pop and return per-channel outbound message IDs for the last result.

    Returns None if no IDs were stashed (no text result was sent).
    """
    return _last_result_ids.pop(chat_jid, None)
```

Add `pop_last_result_ids` and `_last_result_ids` to `__all__`.

**Step 10: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_messaging_router.py::TestHandleStreamedOutput::test_final_result_stashes_outbound_ids -xvs 2>&1 | tail -20`

Expected: PASS

**Step 11: Run full router test suite**

Run: `uv run python -m pytest tests/test_messaging_router.py -x 2>&1 | tail -10`

Expected: All pass.

**Step 12: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/router.py tests/test_messaging_router.py
git commit -m "feat: stash per-channel outbound message IDs for post-run reactions"
```

---

### Task 4: Zzz reaction — channel handler helper

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/channel_handler.py`
- Test: `tests/test_channel_handler.py`

We need a new function that sends a reaction to outbound messages using per-channel IDs (unlike `send_reaction_to_channels` which uses a single canonical inbound message ID).

**Step 13: Write the failing test**

Add to `tests/test_channel_handler.py`:

```python
# ---------------------------------------------------------------------------
# send_reaction_to_outbound
# ---------------------------------------------------------------------------


class TestSendReactionToOutbound:
    @pytest.mark.asyncio
    async def test_sends_reaction_with_per_channel_ids(self):
        ch = _make_channel(name="slack", connected=True, has_reaction=True)
        deps = _make_deps([ch])
        per_channel_ids = {"slack": "1234567890.000001"}

        await send_reaction_to_outbound(deps, "group@g.us", per_channel_ids, "zzz")

        ch.send_reaction.assert_awaited_once_with(
            "group@g.us", "slack-1234567890.000001", "", "zzz"
        )

    @pytest.mark.asyncio
    async def test_skips_channels_without_ids(self):
        ch = _make_channel(name="slack", connected=True, has_reaction=True)
        deps = _make_deps([ch])
        per_channel_ids = {"other-channel": "1234567890.000001"}

        await send_reaction_to_outbound(deps, "group@g.us", per_channel_ids, "zzz")

        ch.send_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_channels_without_send_reaction(self):
        ch = _make_channel(name="tui", connected=True, has_reaction=False)
        deps = _make_deps([ch])
        per_channel_ids = {"tui": "some-id"}

        await send_reaction_to_outbound(deps, "group@g.us", per_channel_ids, "zzz")
        # No error, no call
```

Add the import at the top of the test file:

```python
from pynchy.host.orchestrator.messaging.channel_handler import (
    send_reaction_to_channels,
    send_reaction_to_outbound,
    set_typing_on_channels,
)
```

**Step 14: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_channel_handler.py::TestSendReactionToOutbound -xvs 2>&1 | tail -20`

Expected: FAIL — `send_reaction_to_outbound` doesn't exist.

**Step 15: Implement the helper**

In `channel_handler.py`, add after `send_reaction_to_channels`:

```python
async def send_reaction_to_outbound(
    deps: ChannelDeps,
    chat_jid: str,
    per_channel_ids: dict[str, str],
    emoji: str,
) -> None:
    """Send a reaction to an outbound message using per-channel message IDs.

    Unlike ``send_reaction_to_channels`` (which takes a single canonical
    inbound message ID), this accepts a mapping of channel_name → raw_ts
    from the streaming pipeline.  Each channel's raw ts is wrapped as
    ``slack-{ts}`` so ``send_reaction`` can extract it.
    """
    for ch in deps.channels:
        ch_name = getattr(ch, "name", "?")
        raw_ts = per_channel_ids.get(ch_name)
        if not raw_ts:
            continue
        if not ch.is_connected() or not hasattr(ch, "send_reaction"):
            continue
        target_jid = resolve_target_jid(chat_jid, ch)
        if not target_jid:
            continue
        try:
            await ch.send_reaction(target_jid, f"slack-{raw_ts}", "", emoji)
        except (OSError, TimeoutError, ConnectionError) as exc:
            logger.debug("Outbound reaction send failed", channel=ch_name, err=str(exc))
```

Note: The `f"slack-{raw_ts}"` wrapping is Slack-specific. If other channels ever support `send_reaction` and `post_message`, their ID format would differ. This is fine for now — Slack is the only channel that supports both. Add a comment noting this coupling.

**Step 16: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_channel_handler.py::TestSendReactionToOutbound -xvs 2>&1 | tail -20`

Expected: PASS

**Step 17: Run full channel handler tests**

Run: `uv run python -m pytest tests/test_channel_handler.py -x 2>&1 | tail -10`

Expected: All pass.

**Step 18: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/channel_handler.py tests/test_channel_handler.py
git commit -m "feat: add send_reaction_to_outbound helper for per-channel outbound reactions"
```

---

### Task 5: Zzz reaction — wire into pipeline

**Files:**
- Modify: `src/pynchy/host/orchestrator/messaging/pipeline.py:446-448`
- Modify: `src/pynchy/host/orchestrator/app.py` (add `send_reaction_to_outbound` delegation)
- Test: `tests/test_message_handler.py`

**Step 19: Write the failing test**

Add to `tests/test_message_handler.py`, in the class that tests `process_group_messages` (look for the test at line 670 that asserts on the lobster reaction):

```python
@pytest.mark.asyncio
async def test_zzz_reaction_after_successful_run(self):
    """After a successful agent run that produced output, a :zzz:
    reaction should be sent on the agent's final response."""
    jid = "g@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})

    msg = _make_message("hello", id="msg-42", timestamp="new-ts")

    deps.run_agent = AsyncMock(return_value="success")
    deps.handle_streamed_output = AsyncMock(return_value=True)
    deps.send_reaction_to_outbound = AsyncMock()

    fake_ids = {"slack": "1234567890.000001"}

    with (
        patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
        patch(_PR_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
        patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        patch(
            "pynchy.host.orchestrator.messaging.pipeline.pop_last_result_ids",
            return_value=fake_ids,
        ),
    ):
        result = await process_group_messages(deps, jid)

    assert result is True
    deps.send_reaction_to_outbound.assert_awaited_once_with(
        jid, fake_ids, "zzz"
    )
```

Note: `deps.send_reaction_to_outbound` is a new method on `MessageHandlerDeps`. Add it to the `_make_deps` helper as `AsyncMock()`.

**Step 20: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_message_handler.py -k test_zzz_reaction_after_successful_run -xvs 2>&1 | tail -20`

Expected: FAIL — `send_reaction_to_outbound` doesn't exist on deps, `pop_last_result_ids` not imported.

**Step 21: Wire the pipeline**

In `pipeline.py`, add the import near the top (with other router imports):

```python
from pynchy.host.orchestrator.messaging.router import pop_last_result_ids
```

Add `send_reaction_to_outbound` to the `MessageHandlerDeps` protocol:

```python
    async def send_reaction_to_outbound(
        self, chat_jid: str, per_channel_ids: dict[str, str], emoji: str
    ) -> None: ...
```

In `process_group_messages()`, after the typing indicator goes off (line 447-448) and before the log statement (line 450), add:

```python
    # Send zzz reaction on the agent's final response to indicate sleep
    outbound_ids = pop_last_result_ids(chat_jid)
    if outbound_ids and output_sent_to_user:
        await deps.send_reaction_to_outbound(chat_jid, outbound_ids, "zzz")
```

In `app.py`, add the delegation method to the PynchyApp class (alongside the existing `send_reaction_to_channels` method around line 176):

```python
    async def send_reaction_to_outbound(
        self, chat_jid: str, per_channel_ids: dict[str, str], emoji: str
    ) -> None:
        await channel_handler.send_reaction_to_outbound(self, chat_jid, per_channel_ids, emoji)
```

In the test helper `_make_deps()` in `tests/test_message_handler.py`, add:

```python
    deps.send_reaction_to_outbound = AsyncMock()
```

**Step 22: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_message_handler.py -k test_zzz_reaction_after_successful_run -xvs 2>&1 | tail -20`

Expected: PASS

**Step 23: Run full test suite**

Run: `uv run python -m pytest tests/test_message_handler.py tests/test_messaging_router.py tests/test_channel_handler.py -x 2>&1 | tail -10`

Expected: All pass.

**Step 24: Commit**

```bash
git add src/pynchy/host/orchestrator/messaging/pipeline.py src/pynchy/host/orchestrator/app.py tests/test_message_handler.py
git commit -m "feat: send zzz reaction on agent's final response when container goes to sleep"
```

---

### Task 6: Final verification

**Step 25: Run full test suite**

Run: `uv run python -m pytest -x 2>&1 | tail -20`

Expected: All pass.

**Step 26: Run linting**

Run: `uvx ruff check src/pynchy/host/orchestrator/messaging/inbound.py src/pynchy/host/orchestrator/messaging/pipeline.py src/pynchy/host/orchestrator/messaging/router.py src/pynchy/host/orchestrator/messaging/channel_handler.py src/pynchy/host/orchestrator/app.py`

Expected: No errors.

**Step 27: Verify no type errors in modified files**

Run: `uvx pyright src/pynchy/host/orchestrator/messaging/inbound.py src/pynchy/host/orchestrator/messaging/pipeline.py src/pynchy/host/orchestrator/messaging/router.py src/pynchy/host/orchestrator/messaging/channel_handler.py src/pynchy/host/orchestrator/app.py 2>&1 | tail -10`

Expected: No errors (or only pre-existing ones).
