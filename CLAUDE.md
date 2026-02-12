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

Service management:
```bash
launchctl load ~/Library/LaunchAgents/com.pynchy.plist
launchctl unload ~/Library/LaunchAgents/com.pynchy.plist
```

## Container Build Cache

Apple Container's buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild:

```bash
container builder stop && container builder rm && container builder start
./container/build.sh
```

Always verify after rebuild: `container run -i --rm --entrypoint python pynchy-agent:latest -c "import agent_runner; print('OK')"`
