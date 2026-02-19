---
name: Pynchy Ops
description: Use when managing the pynchy service on the server — deploying changes, observing logs, checking service status, restarting the service, setting up GitHub auth, rebuilding the agent container, or running any commands on pyncher-server via SSH. Also use when interacting with the LiteLLM proxy — investigating failed requests, model routing errors, spend tracking, health checks, API gateway diagnostics, or modifying the LiteLLM configuration. Also use when the user mentions the LiteLLM UI, dashboard, proxy errors, or model availability.
---

# Pynchy Ops

The pynchy service runs on `pyncher-server` over Tailscale. SSH: `ssh pyncher-server`.

## Auto-deploy: Never Restart Manually

Pynchy self-manages. Two mechanisms trigger automatic restarts:

1. **Git changes on `main`** — the polling mechanism detects new commits, pulls, and restarts (with container rebuild if source files changed).
2. **Config file changes** — editing `config.toml`, `litellm_config.yaml`, or other settings files triggers an automatic restart. Edit the file and wait ~30–90s.

**Do not manually restart containers or the service.** This includes `docker restart`, `systemctl restart`, and direct container management (`docker kill/stop/rm`). Manual restarts bypass lifecycle management and can leave things in a bad state.

Only use manual commands when the service is unhealthy and needs fixing. See [references/server-debug.md](references/server-debug.md) for diagnostic steps.

## Quick Status Check

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
curl -s -X POST http://pyncher-server:8484/deploy

# Observe (always safe)
ssh pyncher-server 'systemctl --user status pynchy'
ssh pyncher-server 'journalctl --user -u pynchy -f'
ssh pyncher-server 'journalctl --user -u pynchy -n 100'
ssh pyncher-server 'docker ps --filter name=pynchy'

# Manual restart — ONLY for unhealthy/stuck service
ssh pyncher-server 'systemctl --user restart pynchy'
```

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
ssh -t pyncher-server 'gh auth login -p ssh'

# Verify
ssh pyncher-server 'gh auth status'
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

Master key: `ssh pyncher-server 'grep master_key ~/src/PERSONAL/pynchy/config.toml'`
Pass as: `Authorization: Bearer <key>`

Config: `~/src/PERSONAL/pynchy/litellm_config.yaml`. Editing it triggers an automatic restart (~30–90s). Do not manually restart containers.

Dashboard: `http://pyncher-server:4000/ui/`

- **Diagnostics, spend tracking, failure analysis**: [references/litellm-diagnostics.md](references/litellm-diagnostics.md)
- **MCP server management API and gotchas**: [references/litellm-mcp-api.md](references/litellm-mcp-api.md)

## MCP Server Containers

MCP tool servers (e.g., Playwright) run as separate Docker containers managed by `McpManager`. They start on-demand when an agent needs them and stop after the configured `idle_timeout`.

See `src/pynchy/container_runner/mcp_manager.py` and [MCP management](../docs/architecture/mcp-management.md).

## Server Debugging

For specific failure scenarios — container timeouts, agent not responding, mount issues, WhatsApp auth — see [references/server-debug.md](references/server-debug.md).

Docker logs are useful for runtime errors (container crashes, process failures) where the issue occurs before messages reach the database. For agent behavior, use the `pynchy-dev` skill's SQLite query reference instead.
