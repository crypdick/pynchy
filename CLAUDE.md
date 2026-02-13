# Pynchy

Personal Claude assistant. See [README.md](README.md) for philosophy and setup. See [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) for architecture decisions.

## Quick Context

Single Python process that connects to WhatsApp, routes messages to Claude Agent SDK running in containers (Apple Container on macOS, Docker on Linux). Each group has isolated filesystem and memory.

## Key Files

| File | Purpose |
|------|---------|
| `src/pynchy/db.py` | SQLite operations (async, aiosqlite) |
| `src/pynchy/ipc.py` | IPC watcher and task processing |
| `src/pynchy/router.py` | Message formatting and outbound routing |
| `src/pynchy/config.py` | Trigger pattern, paths, intervals |
| `src/pynchy/group_queue.py` | Per-group queue with global concurrency limit |
| `src/pynchy/runtime.py` | Container runtime detection (Apple Container / Docker) |
| `src/pynchy/mount_security.py` | Mount path validation and allowlist |
| `src/pynchy/task_scheduler.py` | Runs scheduled tasks |
| `src/pynchy/types.py` | Data models (dataclasses) |
| `src/pynchy/logger.py` | Structured logging (structlog) |
| `groups/{name}/CLAUDE.md` | Per-group memory (isolated) |
| `container/skills/agent-browser.md` | Browser automation tool (available to all agents via Bash) |
| `docs/backlog/TODO.md` | Work item index — one-line items linking to plan files in status folders |

## Skills

| Skill | When to Use |
|-------|-------------|
| `/setup` | First-time installation, authentication, service configuration |
| `/customize` | Adding channels, integrations, changing behavior |
| `/debug` | Container issues, logs, troubleshooting |

## Development

Run commands directly—don't tell the user to run them.

```bash
uv run pynchy            # Run the app
uv run pytest tests/     # Run tests
uv run ruff check --fix src/ container/agent_runner/src/  # Lint + autofix
uv run ruff format src/ container/agent_runner/src/       # Format
uvx pre-commit run --all-files  # Run all pre-commit hooks
./container/build.sh     # Rebuild agent container
```

Service management (macOS):
```bash
launchctl load ~/Library/LaunchAgents/com.pynchy.plist
launchctl unload ~/Library/LaunchAgents/com.pynchy.plist
```

Service management (Linux):
```bash
systemctl --user start pynchy
systemctl --user stop pynchy
systemctl --user restart pynchy
journalctl --user -u pynchy -f          # Follow logs
```

## Deployment

The pynchy service runs on `nuc-server` (Intel NUC, headless Ubuntu) over Tailscale. SSH: `ssh nuc-server`, user `ricardo`.

```bash
# Deploy / restart
curl -s -X POST http://nuc-server:8484/deploy

# Service status & logs (run on NUC or via ssh)
ssh nuc-server 'systemctl --user status pynchy'
ssh nuc-server 'journalctl --user -u pynchy -f'
ssh nuc-server 'journalctl --user -u pynchy -n 100'

# Check running containers
ssh nuc-server 'docker ps --filter name=pynchy'
```

## Container GitHub Access

Container agents get GitHub credentials auto-forwarded from the host's `gh` CLI. To set up on a new host:

```bash
# Interactive login (works over SSH with -t for TTY)
ssh -t nuc-server 'gh auth login -p ssh'
# Select: GitHub.com → Login with a web browser
# Then follow the device code flow in your local browser

# Verify
ssh nuc-server 'gh auth status'
```

After authenticating, `_write_env_file()` auto-discovers `GH_TOKEN` and git identity on each container launch. No manual env configuration needed.

## Container Build Cache

Apple Container's buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild:

```bash
container builder stop && container builder rm && container builder start
./container/build.sh
```

Always verify after rebuild: `container run -i --rm --entrypoint python pynchy-agent:latest -c "import agent_runner; print('OK')"`
