---
name: Pynchy Ops
description: Use when managing the pynchy service on the server — deploying changes, observing logs, checking service status, restarting the service, setting up GitHub auth, rebuilding the agent container, or running commands on the live Pynchy host. Also use when interacting with the LiteLLM proxy — investigating failed requests, model routing errors, spend tracking, health checks, API gateway diagnostics, or modifying the LiteLLM configuration. Also use when the user mentions the LiteLLM UI, dashboard, proxy errors, or model availability.
---

# Pynchy Ops

The live Pynchy host and checkout path are deployment-specific. Public repo instructions must not assume a private hostname or home-directory layout. Set `PYNCHY_HOST` and `PYNCHY_REMOTE_ROOT` from local memory, environment, or the operator before running remote commands.

## Auto-deploy: Never Restart Manually

Pynchy self-manages. Two mechanisms trigger automatic restarts:

1. **Git changes on `main`** — the polling mechanism detects new commits, pulls, and restarts (with container rebuild if source files changed).
2. **Config file changes** — editing `config.toml`, `litellm_config.yaml`, or other settings files triggers an automatic deploy on the next host git-sync poll. The default interval is 300 seconds; check `[scheduler].git_sync_interval_seconds` before deciding it was missed.

**Do not manually restart containers or the service.** This includes `docker restart`, `systemctl restart`, and direct container management (`docker kill/stop/rm`). Manual restarts bypass lifecycle management and can leave things in a bad state.

Only use manual commands when the service is unhealthy and needs fixing. See [references/server-debug.md](references/server-debug.md) for diagnostic steps.

## Quick Status Check

**Preferred: the `/status` endpoint.** Single command that returns everything:

```bash
# On the live host directly:
curl -s http://localhost:8484/status | python3 -m json.tool

# Remotely (via Tailscale):
PYNCHY_HOST="${PYNCHY_HOST:?set the live host}"
PYNCHY_PORT="${PYNCHY_PORT:-8484}"
curl -s "http://$PYNCHY_HOST:$PYNCHY_PORT/status" | python3 -m json.tool
```

Returns JSON with: `service` (uptime), `deploy` (SHA, dirty, unpushed), `channels` (slack/whatsapp connected), `gateway` (LiteLLM health), `temporal` (cluster health, worker state, task queue, last scheduled workflow/result), `queue` (active containers, waiting groups), `repos` (per-repo worktree status — SHA, dirty, ahead/behind, conflicts), `messages` (inbound/outbound counts, last activity), `tasks` (scheduled tasks with status/next run), `host_jobs`, `groups` (total, active sessions).

**Fallback: manual commands** (when the HTTP server is down or you need logs):

```bash
# 1. Is the service running? (macOS live host)
launchctl print "gui/$(id -u)/com.pynchy"

# 2. Any running containers?
docker ps --filter name=pynchy

# 3. Any stopped/orphaned containers?
docker ps -a --filter name=pynchy

# 4. Recent errors in service log?
tail -n 100 "$PYNCHY_REMOTE_ROOT/logs/pynchy.error.log"

# 5. Is Slack/WhatsApp connected?
tail -n 200 "$PYNCHY_REMOTE_ROOT/logs/pynchy.log" | grep -E 'Connected to|Connection closed|Slack'

# 6. Are groups loaded?
tail -n 200 "$PYNCHY_REMOTE_ROOT/logs/pynchy.log" | grep groupCount
```

## Deploy & Observe

```bash
# Trigger a deploy (from HOST — use mcp__pynchy__deploy_changes from containers)
PYNCHY_HOST="${PYNCHY_HOST:?set the live host}"
PYNCHY_PORT="${PYNCHY_PORT:-8484}"
PYNCHY_REMOTE_ROOT="${PYNCHY_REMOTE_ROOT:?set the live checkout path}"
curl -s -X POST "http://$PYNCHY_HOST:$PYNCHY_PORT/deploy"

# Observe (always safe)
ssh "$PYNCHY_HOST" 'launchctl print gui/$(id -u)/com.pynchy'
ssh "$PYNCHY_HOST" "tail -n 100 '$PYNCHY_REMOTE_ROOT/logs/pynchy.log'"
ssh "$PYNCHY_HOST" "tail -n 100 '$PYNCHY_REMOTE_ROOT/logs/pynchy.error.log'"
ssh "$PYNCHY_HOST" 'docker ps --filter name=pynchy'

# Manual restart — ONLY for unhealthy/stuck service
ssh "$PYNCHY_HOST" 'launchctl kickstart -k gui/$(id -u)/com.pynchy'
```

## Monitoring Live Agent Activity

**Service logs only show lifecycle events** (container spawn, session create/destroy, errors). They do NOT show agent output (tool calls, thinking, text broadcasts). To monitor what an agent is actually doing, query SQLite:

```bash
# Recent activity for a specific group (replace <JID> with e.g. slack:C0AFR6DB0FK)
ssh "$PYNCHY_HOST" "cd '$PYNCHY_REMOTE_ROOT' && sqlite3 data/messages.db \"
  SELECT timestamp, message_type, substr(content, 1, 120)
  FROM messages WHERE chat_jid = '<JID>'
  ORDER BY timestamp DESC LIMIT 15;
\""

# All recent activity across all groups
ssh "$PYNCHY_HOST" "cd '$PYNCHY_REMOTE_ROOT' && sqlite3 data/messages.db \"
  SELECT timestamp, chat_jid, message_type, substr(content, 1, 80)
  FROM messages ORDER BY timestamp DESC LIMIT 15;
\""
```

## Temporal Scheduler

Scheduled work runs through Temporal. Pynchy reconciles active agent tasks, database host jobs, and config cron jobs into Temporal schedules or delayed workflows. Pynchy owns the worker in the host process; Temporal owns workflow durability and wake-ups.

macOS launchd deployment:

| Item | Value |
|------|-------|
| LaunchAgent | `~/Library/LaunchAgents/com.pynchy.temporal.plist` |
| Address | `127.0.0.1:7233` |
| DB | `$PYNCHY_REMOTE_ROOT/data/temporal.db` |
| Logs | `~/Library/Logs/pynchy/temporal.log`, `~/Library/Logs/pynchy/temporal.err.log` |

Safe checks:

```bash
ssh "$PYNCHY_HOST" 'launchctl print gui/$(id -u)/com.pynchy.temporal'
ssh "$PYNCHY_HOST" 'temporal operator cluster health --address 127.0.0.1:7233'
ssh "$PYNCHY_HOST" 'lsof -nP -iTCP:7233 -sTCP:LISTEN'
curl -s "http://$PYNCHY_HOST:${PYNCHY_PORT:-8484}/status" | python3 -m json.tool
```

`data/temporal.db` is durable scheduler state. Make sure host backups include it with the rest of `data/`.

## Runtime DB Backups

macOS deployments can use `scripts/backup_runtime_dbs.sh` for SQLite-safe runtime DB snapshots. It backs up `messages.db`, `memories.db`, `neonize.db`, and `temporal.db` into iCloud Drive by default.

Live service:

| Item | Value |
|------|-------|
| LaunchAgent | `~/Library/LaunchAgents/com.pynchy.backup.plist` |
| Destination | `~/Library/Mobile Documents/com~apple~CloudDocs/PynchyBackups` |
| Logs | `~/Library/Logs/pynchy/backup.log`, `~/Library/Logs/pynchy/backup.err.log` |

Safe checks:

```bash
ssh "$PYNCHY_HOST" 'launchctl print gui/$(id -u)/com.pynchy.backup'
ssh "$PYNCHY_HOST" 'ls -lt ~/Library/Mobile\ Documents/com~apple~CloudDocs/PynchyBackups | head'
ssh "$PYNCHY_HOST" 'tail -n 50 ~/Library/Logs/pynchy/backup.err.log'
```

**When to use what:**

| What you need | Tool |
|---------------|------|
| Is the service running? | `launchctl print gui/$(id -u)/com.pynchy` |
| Did the container spawn/crash? | launchd logs or `docker logs` |
| What is the agent doing right now? | **SQLite** `messages` table |
| Agent tool calls and traces | **SQLite** `events` table |
| Container startup errors (before DB writes) | `docker logs pynchy-<group>` |

## Sending Synthetic Messages

Use the TUI API to inject messages into any group's chat pipeline (useful for testing):

```bash
# Send a message as if a user typed it
curl -s -X POST "http://$PYNCHY_HOST:${PYNCHY_PORT:-8484}/api/send" \
  -H "Content-Type: application/json" \
  -d '{"jid": "<JID>", "content": "your message here"}'
```

This goes through the full message pipeline (routing → agent → output → broadcast), same as a real Slack/WhatsApp message.

## Service Management Reference

macOS:
```bash
launchctl load ~/Library/LaunchAgents/com.pynchy.plist
launchctl unload ~/Library/LaunchAgents/com.pynchy.plist
```

Linux:
```bash
systemctl --user start pynchy
systemctl --user stop pynchy
systemctl --user restart pynchy
journalctl --user -u pynchy -f          # Follow logs
```

Systemd unit template: `config-examples/pynchy.service.EXAMPLE`

## Container GitHub Access

**Admin containers only.** `GH_TOKEN` is forwarded only to admin containers. Non-admin containers have git operations routed through host IPC and never receive the token.

```bash
# Interactive login (works over SSH with -t for TTY)
ssh -t "$PYNCHY_HOST" 'gh auth login -p ssh'

# Verify
ssh "$PYNCHY_HOST" 'gh auth status'
```

After authenticating, `_write_env_file()` auto-discovers `GH_TOKEN` and git identity on each admin container launch. No manual env configuration needed.

## Container Build Cache

Apple Container's buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps. To force a truly clean rebuild:

```bash
container builder stop && container builder rm && container builder start
./src/pynchy/agent/build.sh
```

Verify: `container run -i --rm --entrypoint python pynchy-agent:latest -c "import agent_runner; print('OK')"`

## LiteLLM Gateway

Runs as `pynchy-litellm` Docker container with PostgreSQL sidecar (`pynchy-litellm-db`). Access at `http://localhost:4000` on the Pynchy host, or via Tailscale at port 4000.

Master key lookup: `ssh "$PYNCHY_HOST" "grep master_key '$PYNCHY_REMOTE_ROOT/config.toml'"`
Pass as: `Authorization: Bearer <key>`

If `master_key` is not in `config.toml`, it may be injected via `.env` or container env. Prefer a scripted lookup that **does not print the key**, e.g. using it inline for a request (see `references/litellm-diagnostics.md` for examples).

Config: `$PYNCHY_REMOTE_ROOT/litellm_config.yaml`. Editing it triggers an automatic deploy on the next host git-sync poll (300 seconds by default). Do not manually restart containers.

Dashboard: `http://$PYNCHY_HOST:4000/ui/`

- **Diagnostics, spend tracking, failure analysis**: [references/litellm-diagnostics.md](references/litellm-diagnostics.md)
- **MCP server management API and gotchas**: [references/litellm-mcp-api.md](references/litellm-mcp-api.md)

## Zombie Processes (LiteLLM)

If SSH login reports zombie processes, check whether they live inside the LiteLLM container:

```bash
ssh "$PYNCHY_HOST" 'docker exec pynchy-litellm ps -eo pid,ppid,stat,args | awk '\''$3 ~ /Z/ {print}'\'''
```

Note: use `args`, not `cmd` — `cmd` can appear empty for zombie processes.

## MCP Server Containers

MCP tool servers (e.g., Playwright) run as separate Docker containers managed by `McpManager`. They start on-demand when an agent needs them and stop after the configured `idle_timeout`.

See `src/pynchy/host/container_manager/mcp/` and [MCP management](../docs/architecture/mcp-management.md).

## Database Files

All databases live in `data/`:

| File | Purpose |
|------|---------|
| `data/messages.db` | Main DB — messages, groups, sessions, tasks, events, outbound ledger |
| `data/neonize.db` | WhatsApp auth state (Neonize credentials) |
| `data/memories.db` | BM25-ranked memory store (sqlite-memory plugin) |

Quick inspection (run on the live host or prefix with `ssh "$PYNCHY_HOST" "cd '$PYNCHY_REMOTE_ROOT' && ..."`):

```bash
# List registered groups
sqlite3 data/messages.db "SELECT name, folder, is_admin FROM registered_groups;"

# Recent messages across all channels
sqlite3 data/messages.db "SELECT timestamp, chat_jid, sender_name, substr(content, 1, 80) FROM messages ORDER BY timestamp DESC LIMIT 10;"

# Active sessions
sqlite3 data/messages.db "SELECT * FROM sessions;"

# Scheduled tasks
sqlite3 data/messages.db "SELECT id, group_folder, status, next_run FROM scheduled_tasks WHERE status = 'active';"
```

For the full query cookbook (traces, tool calls, cross-table debugging), see the `pynchy-dev` skill's [sqlite-queries.md](../pynchy-dev/references/sqlite-queries.md).

## Server Debugging

For specific failure scenarios — container timeouts, agent not responding, mount issues, WhatsApp auth — see [references/server-debug.md](references/server-debug.md).

Docker logs are useful for runtime errors (container crashes, process failures) where the issue occurs before messages reach the database. For agent behavior, use the `pynchy-dev` skill's SQLite query reference instead.
