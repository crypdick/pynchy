# Bug: Slack reconnect shutdown race (recurrence)

## What happened

Service restart at 22:32 PST on 2026-02-18 crashed with `RuntimeError: Executor shutdown has been called`. The restart was triggered by a `.env` credential update. Service exited with code 1 and required systemd auto-restart to recover.

## Stack trace

```
Slack SDK → aiohttp connector → DNS resolve → asyncio.run_in_executor → executor already shut down
```

`RuntimeError: Executor shutdown has been called` — the asyncio default executor has been torn down while an aiohttp DNS resolution is still in flight.

## Root cause hypothesis

Commit `76065e0` ("fix: cancel pending Slack reconnect task on disconnect to prevent shutdown race") addressed the obvious case: cancelling `_reconnect_task` during `disconnect()`. But the race persists because:

1. `_on_handler_done` fires when the Socket Mode handler task exits unexpectedly
2. It checks `if not self._connected: return` — this guard works when `disconnect()` has already set the flag
3. **But**: if `_on_handler_done` fires *before* `disconnect()` is called (e.g., Slack connection drops moments before service restart), it schedules `_reconnect_with_backoff`
4. The reconnect task sleeps, then calls `self.connect()`, which initializes the Slack SDK and calls `_handler.start_async()`
5. `start_async()` spawns internal aiohttp tasks (WebSocket connection, DNS resolution) that are **not tracked** by `_reconnect_task`
6. When `disconnect()` finally runs, it cancels `_reconnect_task`, but the Slack SDK's internal tasks are orphaned
7. During event loop shutdown, the default executor is torn down while those orphaned tasks are still trying to do DNS resolution via `run_in_executor`

The fundamental issue: **cancelling `_reconnect_task` doesn't propagate to the Slack SDK's internal subtasks**.

## File to investigate

`src/pynchy/chat/plugins/slack.py` — focus on:
- `disconnect()` (line 121)
- `_on_handler_done()` (line 150)
- `_reconnect_with_backoff()` (line 164)

## Potential fix directions

1. **Check `_connected` after sleep in `_reconnect_with_backoff`**: Add `if not self._connected: return` after the `asyncio.sleep(delay)` call and before calling `self.connect()`. This closes the window where a reconnect proceeds after shutdown has begun but before `_reconnect_task` is cancelled.

2. **Full teardown in `disconnect()`**: After cancelling `_reconnect_task`, also call `self._handler.close_async()` and cancel `_handler_task` even if the reconnect was in-flight — the handler/task might have been freshly created by a concurrent reconnect.

3. **Shutdown flag**: Replace `_connected` with a more explicit `_shutting_down` flag that is checked at every async boundary in the reconnect path.

4. **Suppress executor errors during shutdown**: Wrap the reconnect logic in a try/except for `RuntimeError` with the executor message, since it's a benign race during shutdown.

## Desired outcome

Shutdown should never crash with exit code 1, even if Slack reconnect logic is mid-flight. The service should exit cleanly (code 0) regardless of the timing between connection drops, reconnect attempts, and shutdown signals.
