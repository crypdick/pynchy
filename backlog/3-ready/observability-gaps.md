# Observability Gaps — Handoff Doc

**Date:** 2026-02-25
**Context:** User reported pynchy appeared non-functional (scheduled task silent, admin-1 unresponsive). Investigation revealed the system was actually working but the failures were invisible.

## What Happened

Three independent issues occurred simultaneously, creating the appearance of a total outage:

1. **Slack message silently dropped** — User sent "is your local git repo dirty?" to admin-1 after a context reset (`c`). The message never reached pynchy (zero Slack inbound log entries, never stored in DB). Root cause: Slack Socket Mode delivery gap during a restart cycle (service crashed 3x with Pydantic validation error before succeeding). The reconciler exists but didn't catch it.

2. **Scheduled task ran 25 min with zero user visibility** — code-improver's hourly cron fired, spawned a one-shot container that ran for 25 minutes making 23+ API calls. It successfully committed code. But one-shot containers batch-deliver output only AFTER exit — during the run, the user saw nothing. The "⏱ Scheduled task starting" message was sent, but no heartbeat or progress indicator followed.

3. **Boot crash cycle was invisible** — Service crashed 3x in 30 seconds (`caldav.nextcloud.password` extra_forbidden — already fixed). User was never notified about the crash-restart cycle. The boot "🦞 online" message only fires on successful start, so the failures were silent.

## Confirmed Working

- Synthetic message test via `/api/send` worked perfectly — admin-1 spawned and responded in ~10s
- IPC file-based output pipeline is functioning correctly
- LiteLLM gateway healthy (all 200 OK responses)
- Slack Socket Mode connection is stable post-restart
- One-shot container output collection works (batch after exit)

## Three Fixes Needed

### Fix 1: Slack message gap alerting

**Problem:** The reconciler (`chat/reconciler.py`) already fetches missed messages on reconnect, but it doesn't alert the user when it detects a gap.

**Fix:** When `reconcile_all_channels()` recovers missed inbound messages, broadcast a host message to the affected group: "Recovered N missed message(s) from [channel] — processing now." This makes silent Slack drops visible.

**Files:** `src/pynchy/chat/reconciler.py` — in the inbound recovery section, after storing recovered messages, broadcast a notification.

### Fix 2: Scheduled task progress heartbeats

**Problem:** One-shot containers (`run_container_agent()` in `_orchestrator.py`) have no session handler, so the IPC watcher skips their output files (by design — files are left for post-exit collection). This means zero intermediate visibility during long runs.

**Two approaches (pick one):**

**A) Periodic status log (simpler):** In `task_scheduler.py`, after spawning the one-shot container, start a periodic check (every 60s) that queries `docker ps` or the process status and broadcasts a heartbeat to the group: "⏱ Task still running (2m elapsed, container active)". Stop when the container exits.

**B) Watcher-based streaming (better UX but more work):** For one-shot containers, register a temporary output handler in the IPC watcher so output events ARE streamed during the run (same as interactive sessions). This would require `run_container_agent()` to register a session or output handler before waiting for exit, and clean it up after.

**Files:**
- Approach A: `src/pynchy/task_scheduler.py`
- Approach B: `src/pynchy/container_runner/_orchestrator.py`, `src/pynchy/ipc/_watcher.py`, `src/pynchy/container_runner/_session.py`

### Fix 3: Boot failure notification

**Problem:** When the service crashes on startup, the only evidence is in `journalctl`. The user has no Slack notification because the service never reached the point where channels connect.

**Fix:** After successful boot, check the systemd restart counter or detect that previous starts failed. If `restart_counter > expected` (or a crash breadcrumb file exists), include a warning in the boot notification: "⚠️ Service recovered after N failed start(s)". The crash breadcrumb approach: write a file on startup, delete it after successful init. If the file exists on next startup, a previous attempt crashed.

**Files:** `src/pynchy/startup_handler.py` — add crash detection before the boot notification is sent.

## Priority

Fix 1 > Fix 3 > Fix 2. The Slack gap alert is highest-value because it's the only one where user data (messages) is silently lost. Boot failure notification is next because it's cheap and catches config errors. Task heartbeats are nice-to-have — the task DID complete, it just wasn't visible.

## Investigation Notes

- The Pydantic crash was `caldav.nextcloud.password` — the `password` field was renamed to `password_env` in the CalDAV config but the server's `config.toml` still had the old field. Already fixed in `bcd640f`.
- The "No git token for repo" warnings at boot are benign for local repos but would break private repo cloning.
- The daily ~04:00 "Failed to start pynchy.service" entries in journalctl appear to be a daily reboot/restart cycle. The service recovers, but this is the window where scheduled tasks can be missed.
- `restart counter is at 731` — the service has been restarted 731 times total (mostly from auto-deploys and the daily restarts).
