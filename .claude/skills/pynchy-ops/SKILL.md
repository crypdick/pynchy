---
name: Pynchy Ops
description: Use when managing the pynchy service on the server — deploying changes, observing logs, checking service status, restarting the service, setting up GitHub auth, rebuilding the agent container, or running commands on the live Pynchy host. Also use when interacting with the LiteLLM proxy — investigating failed requests, model routing errors, spend tracking, health checks, API gateway diagnostics, or modifying the LiteLLM configuration. Also use when the user mentions the LiteLLM UI, dashboard, proxy errors, or model availability.
---

# Pynchy Ops

The live personal Pynchy service runs on `mac-mini` over Tailscale. SSH: `ssh mac-mini`.

Treat `pynchy-server` as historical unless a fresh status check proves it is serving the live deployment.

## Auto-deploy: Never Restart Manually

Pynchy self-manages. Two mechanisms trigger automatic restarts:

1. **Git changes on `main`** — the polling mechanism detects new commits, pulls, and restarts (with container rebuild if source files changed).
2. **Config file changes** — editing `config.toml`, `litellm_config.yaml`, or other settings files triggers an automatic restart. Edit the file and wait ~30–90s.

**Do not manually restart containers or the service.** This includes `docker restart`, `systemctl restart`, and direct container management (`docker kill/stop/rm`). Manual restarts bypass lifecycle management and can leave things in a bad state.

Only use manual commands when the service is unhealthy and needs fixing. See [references/server-debug.md](references/server-debug.md) for diagnostic steps.

## Quick Status Check

**Preferred: the `/status` endpoint.** Single command that returns everything:

```bash
# On the live host directly:
curl -s http://localhost:8485/status | python3 -m json.tool

# Remotely (via Tailscale):
curl -s http://mac-mini:8485/status | python3 -m json.tool
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
tail -n 100 ~/src/PERSONAL/pynchy/logs/pynchy.error.log

# 5. Is Slack/WhatsApp connected?
tail -n 200 ~/src/PERSONAL/pynchy/logs/pynchy.log | grep -E 'Connected to|Connection closed|Slack'

# 6. Are groups loaded?
tail -n 200 ~/src/PERSONAL/pynchy/logs/pynchy.log | grep groupCount
```

## Deploy & Observe

```bash
# Trigger a deploy (from HOST — use mcp__pynchy__deploy_changes from containers)
curl -s -X POST http://mac-mini:8485/deploy

# Observe (always safe)
ssh mac-mini 'launchctl print gui/$(id -u)/com.pynchy'
ssh mac-mini 'tail -n 100 ~/src/PERSONAL/pynchy/logs/pynchy.log'
ssh mac-mini 'tail -n 100 ~/src/PERSONAL/pynchy/logs/pynchy.error.log'
ssh mac-mini 'docker ps --filter name=pynchy'

# Manual restart — ONLY for unhealthy/stuck service
ssh mac-mini 'launchctl kickstart -k gui/$(id -u)/com.pynchy'
```

## Monitoring Live Agent Activity

**Service logs only show lifecycle events** (container spawn, session create/destroy, errors). They do NOT show agent output (tool calls, thinking, text broadcasts). To monitor what an agent is actually doing, query SQLite:

```bash
# Recent activity for a specific group (replace <JID> with e.g. slack:C0AFR6DB0FK)
ssh mac-mini 'cd ~/src/PERSONAL/pynchy && sqlite3 data/messages.db "
  SELECT timestamp, message_type, substr(content, 1, 120)
  FROM messages WHERE chat_jid = '\''<JID>'\''
  ORDER BY timestamp DESC LIMIT 15;
"'

# All recent activity across all groups
ssh mac-mini 'cd ~/src/PERSONAL/pynchy && sqlite3 data/messages.db "
  SELECT timestamp, chat_jid, message_type, substr(content, 1, 80)
  FROM messages ORDER BY timestamp DESC LIMIT 15;
"'
```

## Temporal Scheduler

Scheduled work runs through Temporal. Pynchy reconciles active agent tasks, database host jobs, and config cron jobs into Temporal schedules or delayed workflows. Pynchy owns the worker in the host process; Temporal owns workflow durability and wake-ups.

mac-mini service:

| Item | Value |
|------|-------|
| LaunchAgent | `~/Library/LaunchAgents/com.pynchy.temporal.plist` |
| Address | `127.0.0.1:7233` |
| DB | `~/src/PERSONAL/pynchy/data/temporal.db` |
| Logs | `~/Library/Logs/pynchy/temporal.log`, `~/Library/Logs/pynchy/temporal.err.log` |

Safe checks:

```bash
ssh mac-mini 'launchctl print gui/$(id -u)/com.pynchy.temporal'
ssh mac-mini 'temporal operator cluster health --address 127.0.0.1:7233'
ssh mac-mini 'lsof -nP -iTCP:7233 -sTCP:LISTEN'
curl -s http://mac-mini:8485/status | python3 -m json.tool
```

`data/temporal.db` is durable scheduler state. Make sure host backups include it with the rest of `data/`.

## Runtime DB Backups

mac-mini uses `scripts/backup_runtime_dbs.sh` for SQLite-safe runtime DB snapshots. It backs up `messages.db`, `memories.db`, `neonize.db`, and `temporal.db` into iCloud Drive by default.

Live service:

| Item | Value |
|------|-------|
| LaunchAgent | `~/Library/LaunchAgents/com.pynchy.backup.plist` |
| Destination | `~/Library/Mobile Documents/com~apple~CloudDocs/PynchyBackups` |
| Logs | `~/Library/Logs/pynchy/backup.log`, `~/Library/Logs/pynchy/backup.err.log` |

Safe checks:

```bash
ssh mac-mini 'launchctl print gui/$(id -u)/com.pynchy.backup'
ssh mac-mini 'ls -lt ~/Library/Mobile\ Documents/com~apple~CloudDocs/PynchyBackups | head'
ssh mac-mini 'tail -n 50 ~/Library/Logs/pynchy/backup.err.log'
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
curl -s -X POST http://mac-mini:8485/api/send \
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
ssh -t mac-mini 'gh auth login -p ssh'

# Verify
ssh mac-mini 'gh auth status'
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

Runs as `pynchy-litellm` Docker container with PostgreSQL sidecar (`pynchy-litellm-db`). Access at `http://localhost:4000` on the pynchy server, or via Tailscale at port 4000.

Master key: `ssh mac-mini 'grep master_key ~/src/PERSONAL/pynchy/config.toml'`
Pass as: `Authorization: Bearer <key>`

If `master_key` is not in `config.toml`, it may be injected via `.env` or container env. Prefer a scripted lookup that **does not print the key**, e.g. using it inline for a request (see `references/litellm-diagnostics.md` for examples).

Config: `~/src/PERSONAL/pynchy/litellm_config.yaml`. Editing it triggers an automatic restart (~30–90s). Do not manually restart containers.

Dashboard: `http://mac-mini:4000/ui/`

- **Diagnostics, spend tracking, failure analysis**: [references/litellm-diagnostics.md](references/litellm-diagnostics.md)
- **MCP server management API and gotchas**: [references/litellm-mcp-api.md](references/litellm-mcp-api.md)

## Zombie Processes (LiteLLM)

If SSH login reports zombie processes, check whether they live inside the LiteLLM container:

```bash
ssh mac-mini 'docker exec pynchy-litellm ps -eo pid,ppid,stat,args | awk '\''$3 ~ /Z/ {print}'\'''
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

Quick inspection (run on the live host or prefix with `ssh mac-mini 'cd ~/src/PERSONAL/pynchy && ...'`):

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
