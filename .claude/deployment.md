# Deployment

The pynchy service runs on `nuc-server` (Intel NUC, headless Ubuntu) over Tailscale. SSH: `ssh nuc-server`, user `ricardo`.

Pushing to `main` is all that's needed — the prod server auto-pulls and restarts the service. No manual deploy required.

**Never SSH into the server to run `git pull` manually.** The polling mechanism detects changes on `main` and handles the pull + restart itself. Manual pulls bypass the deploy lifecycle (graceful shutdown, process restart, health checks) and can leave the service in a bad state. Only SSH for `git pull` if you have a specific reason (e.g. debugging a deploy failure).

```bash
# Manual deploy / restart from HOST only (not from containers — use mcp__pynchy__deploy_changes instead)
curl -s -X POST http://nuc-server:8484/deploy

# Service status & logs (run on NUC or via ssh)
ssh nuc-server 'systemctl --user status pynchy'
ssh nuc-server 'journalctl --user -u pynchy -f'
ssh nuc-server 'journalctl --user -u pynchy -n 100'

# Check running containers
ssh nuc-server 'docker ps --filter name=pynchy'
```

## Service Management

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
ssh -t nuc-server 'gh auth login -p ssh'
# Select: GitHub.com → Login with a web browser
# Then follow the device code flow in your local browser

# Verify
ssh nuc-server 'gh auth status'
```

After authenticating, `_write_env_file()` auto-discovers `GH_TOKEN` and git identity on each god container launch. No manual env configuration needed.

## Container Build Cache

Apple Container's buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild:

```bash
container builder stop && container builder rm && container builder start
./container/build.sh
```

Always verify after rebuild: `container run -i --rm --entrypoint python pynchy-agent:latest -c "import agent_runner; print('OK')"`
