# Pynchy

Personal Claude assistant. See [README.md](README.md) for philosophy and setup. See [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) for architecture decisions.

## Quick Context

Python process that connects to WhatsApp, routes messages to Claude Agent SDK running in containers (Apple Container on macOS, Docker on Linux). Each group has isolated filesystem and memory.

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
| `src/pynchy/worktree.py` | Git worktree isolation for non-main project_access groups |
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

## Plugin Security Model

All plugin Python code runs on the host during discovery (`__init__`, `validate()`, category methods). Installing a plugin = trusting its code. Risk by category:

| Category | Sandbox level | Risk | Why |
|----------|--------------|------|-----|
| **Channel** | None — runs persistently in host process | **Highest** | Full filesystem, network, and runtime access for app lifetime |
| **Skill** | Partial — `skill_paths()` on host, content in container | **Medium** | Host method can read arbitrary paths or have side effects |
| **Hook** | Partial — class on host, hook code in container | **Medium** | Host-controlled module path; container code runs with `bypassPermissions` |
| **MCP** | Mostly sandboxed — spec on host, server in container (read-only mount) | **Lower** | Brief host execution; server isolated in container |

**Rule: only install plugins from authors you trust.** See `plugin/base.py` docstring for full details.

## Worktree Isolation

Non-main groups with `project_access` (e.g. code-improver) get their own git worktree at `~/.config/pynchy/worktrees/{group}/` instead of mounting the shared project root. This prevents concurrent containers from editing the same files.

**Sync behavior:** Existing worktrees use best-effort `git fetch` + `git merge`, never `git reset --hard`. A service restart kills all running containers, so agents may leave uncommitted work in their worktree. That state is preserved and reported via system notices so the agent can resume gracefully.

**Post-run merge:** After a successful container run, worktree commits are fast-forward merged into the main branch and pushed. Non-fast-forward merges are logged but not forced.

## Documentation Philosophy

Write documentation from the **user's perspective and goal**, not chronological order. The user is trying to achieve something—help them achieve it by disclosing information when it makes sense in their pursuit of that goal.

**Bad (chronological):** "First we added X, then we refactored Y, then we discovered Z needed changing..."

**Good (goal-oriented):** "To accomplish [goal], do [steps]. Note: [context when relevant to the task]."

Structure documentation around:
- What the user is trying to do
- What they need to know to do it
- Relevant context disclosed at the point of need
- Not the history of how the code evolved

## Code Comments: Capture User Reasoning

When the user gives an instruction or makes a design decision **and explains their reasoning**, capture that reasoning as a comment in the code — right where the decision is implemented. Future maintainers should be able to understand the intent without leaving the code context.

- Only add comments when the user provides a *reason*, not for every instruction
- Place the comment at the point of implementation, not in a separate doc
- Preserve the user's reasoning faithfully — don't paraphrase away the nuance

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

## Documentation Lookup

When you need documentation for a library or framework, use the context7 MCP server to get up-to-date docs. Don't rely on training data for API details that may have changed.

## Testing Philosophy

Write tests that validate **actual business logic**, not just line coverage.

### Good Tests (Real Value)
✅ Test functions with complex branching logic (multiple if/else paths)
✅ Test critical user-facing behavior (message parsing, context resets, formatting)
✅ Test edge cases that could cause bugs (empty inputs, None values, truncation)
✅ Test error conditions and how they're handled
✅ Test data transformations and validation logic
✅ Use descriptive test names that explain what's being validated

### Coverage Theater (Avoid)
❌ Testing trivial getters/setters with no logic
❌ Testing framework-provided functionality (e.g., dataclass equality)
❌ Writing tests just to hit a coverage percentage
❌ Mocking everything so heavily that you're testing the mocks, not real code
❌ Testing implementation details instead of behavior
❌ Tests that would pass even if the code were completely broken

### Examples

**Good:** Testing `is_context_reset()` because:
- Complex logic with multiple valid patterns to match
- Critical business logic (wrong behavior = data loss)
- Many edge cases (case sensitivity, word boundaries, aliases)
- Easy to break with small changes

**Good:** Testing `format_tool_preview()` because:
- Complex branching (different logic per tool type)
- Critical for UX (users need to see what agent is doing)
- Has truncation logic that needs validation
- Many edge cases (None values, special chars, long inputs)

**Coverage Theater:** Testing a simple property accessor:
```python
def test_get_name(self):
    obj = MyClass(name="test")
    assert obj.name == "test"  # Just testing the language works
```

When improving test coverage, focus on **under-tested files with actual logic**:
- Functions with >10 lines and multiple branches
- User-facing features (routing, formatting, triggers)
- Error-prone areas (parsing, validation, state management)
- Code that has caused bugs in the past

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
