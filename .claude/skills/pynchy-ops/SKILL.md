---
name: Pynchy Ops
description: Use when managing the pynchy service on the server — deploying changes, observing logs, checking service status, restarting the service, setting up GitHub auth, rebuilding the agent container, or running commands on the live Pynchy host. Also use when interacting with the LiteLLM proxy — investigating failed requests, model routing errors, spend tracking, health checks, API gateway diagnostics, or modifying the LiteLLM configuration. Also use when the user mentions the LiteLLM UI, dashboard, proxy errors, or model availability.
---

# Pynchy Ops

The live Pynchy host and checkout path are deployment-specific. Public repo instructions must not assume a private hostname or home-directory layout. Set `PYNCHY_HOST` and `PYNCHY_REMOTE_ROOT` from local memory, environment, or the operator before running remote commands.

## Deployment Mode Gate

Identify the live deployment before running an operational command. A `pynchy`
Kubernetes namespace means the Kubernetes path applies:

```bash
PYNCHY_HOST="${PYNCHY_HOST:?set the live host}"
ssh "$PYNCHY_HOST" 'sudo k3s kubectl get namespace pynchy'
```

For Kubernetes deployments:

- Read [Kubernetes installation and operations](../../../docs/installation/kubernetes.md).
- Treat `SCHEDULER__AUTO_DEPLOY=false` as authoritative. A Git pull updates the persistent checkout but does not replace the running image.
- Use `sudo k3s kubectl -n pynchy`; do not use `pynchy deploy`, LaunchAgent commands, standalone systemd commands, or direct container deletion.
- A successful push to `main` publishes immutable images. The namespace-scoped
  `pynchy-release-monitor` CronJob preflights and releases them.
- Do not manually patch the Deployment or delete a Pod for a normal release.
  Observe the monitor Job, rollout, exact release annotation, and application
  status.
- Apply Kubernetes manifest, storage, Secret, RBAC, Pocket TTS, and private MCP
  image changes manually after review; the application release monitor does
  not apply infrastructure.
- Keep unrelated Docker Compose and Dockge services outside Kubernetes.

The standalone sections below apply only when the live host has no Kubernetes
Pynchy Deployment.

## Standalone Auto-deploy: Never Restart Manually

Pynchy self-manages. Two mechanisms trigger automatic restarts:

1. **Git changes on `main`** — the polling mechanism detects new commits, pulls, and restarts (with container rebuild if source files changed).
2. **Config file changes** — editing `config.toml`, `litellm_config.yaml`, or other settings files triggers an automatic deploy on the next host git-sync poll. The default interval is 300 seconds; check `[scheduler].git_sync_interval_seconds` before deciding it was missed.

**Do not manually restart containers or the service.** This includes `docker restart`, `systemctl restart`, and direct container management (`docker kill/stop/rm`). Manual restarts bypass lifecycle management and can leave things in a bad state.

Only use manual commands when the service is unhealthy and needs fixing. See [references/server-debug.md](references/server-debug.md) for diagnostic steps.

## Quick Status Check

Kubernetes deployment:

```bash
ssh "$PYNCHY_HOST" 'sudo k3s kubectl -n pynchy get pods -o wide'
ssh "$PYNCHY_HOST" 'sudo k3s kubectl -n pynchy exec deploy/pynchy -c pynchy -- /opt/pynchy/.venv/bin/pynchy status'
ssh "$PYNCHY_HOST" 'sudo k3s kubectl -n pynchy logs deploy/pynchy -c pynchy --since=30m'
```

The status command uses the permission-restricted Unix socket inside the Pod.
Do not infer application readiness from Kubernetes TCP probes alone; require a
successful status response.

Standalone deployment:

**Preferred: the authenticated control-plane CLI.** It uses the
permission-restricted Unix socket on the live host:

```bash
# On the live host directly:
cd "$PYNCHY_REMOTE_ROOT"
uv run pynchy status

# Remotely over SSH:
PYNCHY_HOST="${PYNCHY_HOST:?set the live host}"
PYNCHY_REMOTE_ROOT="${PYNCHY_REMOTE_ROOT:?set the live checkout path}"
ssh "$PYNCHY_HOST" "cd '$PYNCHY_REMOTE_ROOT' && uv run pynchy status"
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

# 4. Recent errors in the dedicated error log?
tail -n 100 "$PYNCHY_REMOTE_ROOT/logs/pynchy.error.log"

# 4a. Need surrounding application context from macOS launchd?
tail -n 200 "$PYNCHY_REMOTE_ROOT/logs/pynchy.stdout.log"

# 5. Is Slack/WhatsApp connected?
tail -n 200 "$PYNCHY_REMOTE_ROOT/logs/pynchy.stdout.log" | grep -E 'Connected to|Connection closed|Slack'

# 6. Are groups loaded?
tail -n 200 "$PYNCHY_REMOTE_ROOT/logs/pynchy.stdout.log" | grep groupCount
```

## Deploy & Observe

Before deploying source changes, commit one logical change on a feature branch
and merge it into `main`. Do not leave the production checkout dirty or deploy
an uncommitted implementation. Deployment-specific ignored configuration may
change separately when needed, but source changes always go through a commit.

### Kubernetes

Push a tested commit to `main`, then observe the image workflow and
namespace-owned release:

```bash
gh run list --workflow Test --limit 3
ssh "$PYNCHY_HOST" 'sudo k3s kubectl -n pynchy get cronjob pynchy-release-monitor'
ssh "$PYNCHY_HOST" 'sudo k3s kubectl -n pynchy get jobs --sort-by=.metadata.creationTimestamp'
ssh "$PYNCHY_HOST" 'sudo k3s kubectl -n pynchy rollout status deployment/pynchy --timeout=300s'
```

After rollout, verify the full `pynchy.dev/release-sha` Deployment annotation,
Pod image digest, application `last_deploy_sha`, startup warning/error lines,
channel delivery, LiteLLM, and Temporal. Inspect the latest monitor Job logs
when a release does not advance. A failed preflight intentionally leaves the
current Deployment unchanged; a failed rollout or application health check
must show a completed rollback.

### Standalone

```bash
# Trigger a deploy through the live host's Unix socket. From containers, use
# mcp__pynchy__deploy_changes instead.
PYNCHY_HOST="${PYNCHY_HOST:?set the live host}"
PYNCHY_REMOTE_ROOT="${PYNCHY_REMOTE_ROOT:?set the live checkout path}"
ssh "$PYNCHY_HOST" "cd '$PYNCHY_REMOTE_ROOT' && uv run pynchy deploy"

# Observe (always safe)
ssh "$PYNCHY_HOST" 'launchctl print gui/$(id -u)/com.pynchy'
ssh "$PYNCHY_HOST" "tail -n 100 '$PYNCHY_REMOTE_ROOT/logs/pynchy.stdout.log'"
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

Kubernetes deployment:

```bash
ssh "$PYNCHY_HOST" 'sudo k3s kubectl -n pynchy get pods -l app=pynchy-temporal'
ssh "$PYNCHY_HOST" 'sudo k3s kubectl -n pynchy exec deploy/pynchy -c pynchy -- /opt/pynchy/.venv/bin/pynchy status'
```

The Kubernetes manifests provide a dedicated PostgreSQL-backed Temporal
cluster and UI for Pynchy. Do not point Pynchy at an unrelated Compose
Temporal cluster or restore a Temporal SQLite database into PostgreSQL.

Standalone macOS deployment:

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
ssh "$PYNCHY_HOST" "cd '$PYNCHY_REMOTE_ROOT' && uv run pynchy status"
```

`data/temporal.db` is durable scheduler state. Make sure host backups include it with the rest of `data/`.

## Runtime DB Backups

Kubernetes deployments use `deploy/k3s/backup.sh`. It creates SQLite-safe
copies and native LiteLLM, Temporal, and Temporal visibility PostgreSQL dumps.
Back up its generated output; exclude live SQLite files and raw PostgreSQL
directories from file-level backup plans. See
[Kubernetes backup guidance](../../../docs/installation/kubernetes.md#back-up-runtime-state).

Standalone macOS deployments can use `scripts/backup_runtime_dbs.sh` for SQLite-safe runtime DB snapshots. It backs up `messages.db`, `neonize.db`, and `temporal.db` into `data/backups` by default or into the explicitly configured SSH destination. Remote backups stage locally, verify checksums on the destination, and publish atomically. The script briefly unloads and reloads the Temporal LaunchAgent around the `temporal.db` snapshot; never run an online SQLite backup against the active Temporal development server because a write collision can leave its transaction state wedged.

Live service:

| Item | Value |
|------|-------|
| LaunchAgent | `~/Library/LaunchAgents/com.pynchy.backup.plist` |
| Destination | `PYNCHY_BACKUP_REMOTE_HOST:PYNCHY_BACKUP_REMOTE_DIR` from the LaunchAgent |
| Retention | Newest `PYNCHY_BACKUP_KEEP_COUNT` generations, also bounded by `PYNCHY_BACKUP_KEEP_DAYS` |
| Logs | `~/Library/Logs/pynchy/backup.log`, `~/Library/Logs/pynchy/backup.err.log` |

Safe checks:

```bash
ssh "$PYNCHY_HOST" 'launchctl print gui/$(id -u)/com.pynchy.backup'
ssh "$PYNCHY_HOST" 'launchctl print gui/$(id -u)/com.pynchy.backup | grep PYNCHY_BACKUP_'
ssh "$PYNCHY_HOST" 'tail -n 50 ~/Library/Logs/pynchy/backup.log'
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

## Exercising Message Ingress

Pynchy does not expose a production HTTP endpoint for injecting user messages. Send a test
message from a real account through a configured channel so the test crosses the channel's
authentication and ingestion boundaries. Inspect the resulting messages and agent activity in
SQLite as described in [server debugging](references/server-debug.md#exercising-the-message-pipeline).

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

## Workspace GitHub CLI Access

GitHub CLI access requires a selected `type = "workspace"` tool whose
`required_env` includes `GITHUB_TOKEN`. Pynchy does not discover `gh auth`
credentials or inject a broad token into admin agents. Host-side repository
operations retain their separate scoped-token resolution.

Verify only that the managed Pynchy host process receives `GITHUB_TOKEN`; never
print the value. See
[Tool access and secrets](../../../docs/usage/tool-access.md) for the canonical
configuration.

## Production Secret Materialization

Production credentials must enter the managed Pynchy host process through
Proton Pass. Keep `pass://` references in
`data/proton-pass/pynchy.env`; the managed service must start
`scripts/run_pynchy.sh`, which invokes `pass-cli run` when that template
exists. Do not put resolved values in a launchd plist, systemd unit, workspace
file, container argument, or generated env directory.

For unattended SSH diagnostics, load the deployment's ignored provider config
before invoking `pass-cli`:

```bash
cd "$PYNCHY_REMOTE_ROOT"
set -a
. data/proton-pass/host.conf
set +a
pass-cli info
```

Without `host.conf`, `pass-cli` may fall back to a GUI key provider and fail with
`User interaction is not allowed`. Do not ask the operator to approve a GUI
prompt; load the configured headless provider, then rerun the command.

After updating the Pass items or tool requirements, use the normal managed
deployment flow. Verify requirement names and tool availability through status,
logs, or a canary without printing raw task environments or credential values.

## Container Build Cache

Apple Container's buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps. To force a truly clean rebuild:

```bash
container builder stop && container builder rm && container builder start
./src/pynchy/agent/build.sh
```

Verify: `container run -i --rm --entrypoint python pynchy-agent:latest -c "import agent_runner; print('OK')"`

## LiteLLM Gateway

Runs as `pynchy-litellm` Docker container with PostgreSQL sidecar (`pynchy-litellm-db`). Access at `http://localhost:4000` on the Pynchy host, or via Tailscale at port 4000.

Resolve the master key only through an approved secret mechanism into `$KEY`. Never echo, log, or paste it. Pass it only in the `Authorization: Bearer $KEY` header.

Config: `$PYNCHY_REMOTE_ROOT/litellm_config.yaml`. Editing it triggers an automatic deploy on the next host git-sync poll (300 seconds by default). Do not manually restart containers.

Dashboard: `http://$PYNCHY_HOST:4000/ui/`

**Warning:** `/spend/logs` is quarantined for routine live diagnostics regardless of requested limit. Do not use it or `/global/spend/logs` as a substitute.

- **Safe gateway diagnostics, readiness, and failure evidence**: [references/litellm-diagnostics.md](references/litellm-diagnostics.md)
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
