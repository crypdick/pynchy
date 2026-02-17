# Deployment

The pynchy service runs on the `pynchy` host over Tailscale. SSH: `ssh pynchy`.

## Auto-deploy: never restart manually

Pynchy self-manages. Two mechanisms trigger automatic restarts:

1. **Git changes on `main`** — the polling mechanism detects new commits, pulls, and restarts the service (with container rebuild if source files changed).
2. **Config file changes** — editing `config.toml`, `litellm_config.yaml`, or other settings files on the server triggers an automatic restart. Just edit the file and wait ~30–90s for the service to cycle.

**Do not manually restart containers or the service.** This includes `docker restart`, `systemctl restart`, and direct container management (`docker kill/stop/rm`). The service manages its own containers (LiteLLM proxy, PostgreSQL sidecar, agent containers) and will clean up and recreate them on restart. Manual container restarts bypass the lifecycle and can leave things in a bad state.

**Only use manual service commands when the service is unhealthy and needs fixing** — e.g., the deploy endpoint is unreachable, the service is stuck, or you need to debug a crash.

```bash
# Trigger a deploy from HOST (not from containers — use mcp__pynchy__deploy_changes instead)
curl -s -X POST http://pynchy:8484/deploy

# Observe (read-only — always safe)
ssh pynchy 'systemctl --user status pynchy'
ssh pynchy 'journalctl --user -u pynchy -f'
ssh pynchy 'journalctl --user -u pynchy -n 100'
ssh pynchy 'docker ps --filter name=pynchy'

# Manual restart — ONLY for unhealthy/stuck service
ssh pynchy 'systemctl --user restart pynchy'
```

## Service Management (reference)

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

**God containers only.** `GH_TOKEN` is forwarded only to god containers. Non-god containers have git operations routed through host IPC and never receive the token.

To set up GitHub auth on a new host:

```bash
# Interactive login (works over SSH with -t for TTY)
ssh -t pynchy 'gh auth login -p ssh'
# Select: GitHub.com → Login with a web browser
# Then follow the device code flow in your local browser

# Verify
ssh pynchy 'gh auth status'
```

After authenticating, `_write_env_file()` auto-discovers `GH_TOKEN` and git identity on each god container launch. No manual env configuration needed.

## Container Build Cache

Apple Container's buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild:

```bash
container builder stop && container builder rm && container builder start
./container/build.sh
```

Always verify after rebuild: `container run -i --rm --entrypoint python pynchy-agent:latest -c "import agent_runner; print('OK')"`
