---
name: Pynchy Ops
description: Use when managing the pynchy service on the server — deploying changes, observing logs, checking service status, restarting the service, setting up GitHub auth, rebuilding the agent container, or running any commands on pynchy-server via SSH. Also use when interacting with the LiteLLM proxy — investigating failed requests, model routing errors, spend tracking, health checks, API gateway diagnostics, or modifying the LiteLLM configuration. Also use when the user mentions the LiteLLM UI, dashboard, proxy errors, or model availability.
---

# Pynchy Ops

The pynchy service runs on `pynchy-server` over Tailscale. SSH: `ssh pynchy-server`.

## Auto-deploy: Never Restart Manually

Pynchy self-manages. Two mechanisms trigger automatic restarts:

1. **Git changes on `main`** — the polling mechanism detects new commits, pulls, and restarts (with container rebuild if source files changed).
2. **Config file changes** — editing `config.toml`, `litellm_config.yaml`, or other settings files triggers an automatic restart. Edit the file and wait ~30–90s.

**Do not manually restart containers or the service.** This includes `docker restart`, `systemctl restart`, and direct container management (`docker kill/stop/rm`). Manual restarts bypass lifecycle management and can leave things in a bad state.

Only use manual commands when the service is unhealthy and needs fixing. See [references/server-debug.md](references/server-debug.md) for diagnostic steps.

## Quick Status Check

**Preferred: the `/status` endpoint.** Single command that returns everything:

```bash
# On pynchy-server directly:
curl -s http://localhost:8484/status | python3 -m json.tool

# Remotely (via Tailscale):
curl -s http://pynchy-server:8484/status | python3 -m json.tool
```

Returns JSON with: `service` (uptime), `deploy` (SHA, dirty, unpushed), `channels` (slack/whatsapp connected), `gateway` (LiteLLM health, model counts), `queue` (active containers, waiting groups), `repos` (per-repo worktree status — SHA, dirty, ahead/behind, conflicts), `messages` (inbound/outbound counts, last activity), `tasks` (scheduled tasks with status/next run), `host_jobs`, `groups` (total, active sessions).

**Fallback: manual commands** (when the HTTP server is down or you need logs):

```bash
# 1. Is the service running?
systemctl --user status pynchy

# 2. Any running containers?
docker ps --filter name=pynchy

# 3. Any stopped/orphaned containers?
docker ps -a --filter name=pynchy

# 4. Recent errors in service log?
journalctl --user -u pynchy -p err -n 20

# 5. Is WhatsApp connected?
journalctl --user -u pynchy --grep 'Connected to WhatsApp|Connection closed' -n 5

# 6. Are groups loaded?
journalctl --user -u pynchy --grep 'groupCount' -n 3
```

## Deploy & Observe

```bash
# Trigger a deploy (from HOST — use mcp__pynchy__deploy_changes from containers)
curl -s -X POST http://pynchy-server:8484/deploy

# Observe (always safe)
ssh pynchy-server 'systemctl --user status pynchy'
ssh pynchy-server 'journalctl --user -u pynchy -f'
ssh pynchy-server 'journalctl --user -u pynchy -n 100'
ssh pynchy-server 'docker ps --filter name=pynchy'

# Manual restart — ONLY for unhealthy/stuck service
ssh pynchy-server 'systemctl --user restart pynchy'
```

## Monitoring Live Agent Activity

**journalctl only shows lifecycle events** (container spawn, session create/destroy, errors). It does NOT show agent output (tool calls, thinking, text broadcasts). To monitor what an agent is actually doing, query SQLite:

```bash
# Recent activity for a specific group (replace <JID> with e.g. slack:C0AFR6DB0FK)
ssh pynchy-server 'sqlite3 data/messages.db "
  SELECT timestamp, message_type, substr(content, 1, 120)
  FROM messages WHERE chat_jid = '\''<JID>'\''
  ORDER BY timestamp DESC LIMIT 15;
"'

# All recent activity across all groups
ssh pynchy-server 'sqlite3 data/messages.db "
  SELECT timestamp, chat_jid, message_type, substr(content, 1, 80)
  FROM messages ORDER BY timestamp DESC LIMIT 15;
"'
```

**When to use what:**

| What you need | Tool |
|---------------|------|
| Is the service running? | `systemctl --user status pynchy` |
| Did the container spawn/crash? | `journalctl` or `docker logs` |
| What is the agent doing right now? | **SQLite** `messages` table |
| Agent tool calls and traces | **SQLite** `events` table |
| Container startup errors (before DB writes) | `docker logs pynchy-<group>` |

## Sending Synthetic Messages

Use the TUI API to inject messages into any group's chat pipeline (useful for testing):

```bash
# Send a message as if a user typed it
curl -s -X POST http://pynchy-server:8484/api/send \
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
ssh -t pynchy-server 'gh auth login -p ssh'

# Verify
ssh pynchy-server 'gh auth status'
```

After authenticating, `_write_env_file()` auto-discovers `GH_TOKEN` and git identity on each admin container launch. No manual env configuration needed.

## Container Build Cache

Apple Container's buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps. To force a truly clean rebuild:

```bash
container builder stop && container builder rm && container builder start
./container/build.sh
```

Verify: `container run -i --rm --entrypoint python pynchy-agent:latest -c "import agent_runner; print('OK')"`

## LiteLLM Gateway

Runs as `pynchy-litellm` Docker container with PostgreSQL sidecar (`pynchy-litellm-db`). Access at `http://localhost:4000` on the pynchy server, or via Tailscale at port 4000.

Master key: `ssh pynchy-server 'grep master_key ~/src/PERSONAL/pynchy/config.toml'`
Pass as: `Authorization: Bearer <key>`

If `master_key` is not in `config.toml`, it may be injected via `.env` or container env. Prefer a scripted lookup that **does not print the key**, e.g. using it inline for a request (see `references/litellm-diagnostics.md` for examples).

Config: `~/src/PERSONAL/pynchy/litellm_config.yaml`. Editing it triggers an automatic restart (~30–90s). Do not manually restart containers.

Dashboard: `http://pynchy-server:4000/ui/`

- **Diagnostics, spend tracking, failure analysis**: [references/litellm-diagnostics.md](references/litellm-diagnostics.md)
- **MCP server management API and gotchas**: [references/litellm-mcp-api.md](references/litellm-mcp-api.md)

## Zombie Processes (LiteLLM)

If SSH login reports zombie processes, check whether they live inside the LiteLLM container:

```bash
ssh pynchy-server 'docker exec pynchy-litellm ps -eo pid,ppid,stat,args | awk '\''$3 ~ /Z/ {print}'\'''
```

Note: use `args`, not `cmd` — `cmd` can appear empty for zombie processes.

## MCP Server Containers

MCP tool servers (e.g., Playwright) run as separate Docker containers managed by `McpManager`. They start on-demand when an agent needs them and stop after the configured `idle_timeout`.

See `src/pynchy/container_runner/mcp_manager.py` and [MCP management](../docs/architecture/mcp-management.md).

## Database Files

All databases live in `data/`:

| File | Purpose |
|------|---------|
| `data/messages.db` | Main DB — messages, groups, sessions, tasks, events, outbound ledger |
| `data/neonize.db` | WhatsApp auth state (Neonize credentials) |
| `data/memories.db` | BM25-ranked memory store (sqlite-memory plugin) |

Quick inspection (run on pynchy-server or prefix with `ssh pynchy-server`):

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
