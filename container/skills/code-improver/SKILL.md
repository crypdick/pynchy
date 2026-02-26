---
name: code-improver
description: Code improvement guidelines for the pynchy core repository. Finds and fixes concrete code issues in pynchy core.
tier: dev
---

# Pynchy Core Code Improver

## Rules

- If something looks over-engineered, think about how to simplify it
- **Prefer simplification and code removal**: If you find legacy fallbacks, backwards compatibility shims, or deprecated patterns, prefer to delete them and use the latest pattern. Reduce bloat by removing code rather than adding more.
- If there are parallel implementations for the same functionality, consolidate them. But don't be too pedantic about it; if it's just a couple of lines of code that appear in a couple of places, it's usually not worth the effort.
- If a change requires design input, message the human and stop (unless you are triggered by cron)
- Prefix all commits with `[code improver]`
- Run tests before committing: `uv run pytest tests/`
- Fix warnings in tests.
- Run linting before committing: `uv run ruff check --fix src/ container/agent_runner/src/`
- Never make purely cosmetic changes
- Don't make 'god' modules. Files should generally max out around ~450 lines. Files much larger than this should be refactored.
- Keep docs and comments up to date in accordance to the [contributing-docs.md](../../docs/contributing/contributing-docs.md) file.
- making sure plugin-specific code doesn't leak into the core codebase; it should stay with the plugin.
- remove overly defensive try/except blocks that swallow errors. we should only swallow try/except errors for expected errors during normal operation, not to sweep potential bugs under the rug.

## Production Architecture

**You are running in a container environment:**

1. **Host system** - The physical/VM machine
2. **Main Pynchy process** - Runs directly on the host (runs WhatsApp, scheduler, etc.)
3. **Agent container (YOU)** - Your isolated execution environment, spawned by the main process

When you run tests or code, you're executing inside the agent container, which is isolated from the host system. This means:

- System libraries installed on the host (like libmagic1) are NOT accessible to you
- Dependencies must be installed in the agent container's Python environment
- File access is limited to explicitly mounted paths like /workspace
- Tests that require system libraries (e.g., neonize's libmagic dependency) will fail unless those libraries are baked into the agent container image

## Working Directory

The project source is at /workspace/project. Always work from there.
Treat `/workspace/project` as the pynchy core repo and avoid making cross-repo/plugin changes unless explicitly requested.

## Scheduled Run Workflow

When triggered by a scheduled run:

1. Check `git log --oneline -1`. If the last commit message starts with
   "[code improver] no improvements needed", exit immediately - nothing has
   changed since your last run.

2. Run `git log --oneline -20` to see recent history and your past work.

3. Review the codebase for concrete improvements. Pick the single most important
   code improvement you can make in this session and execute it. Possibilities
   include: dead code, duplicated logic, inconsistent patterns, missing error
   handling, type safety gaps, bugs, improved abstractions, improving test
   coverage, or any other kind of code improvement.

4. If something looks like an over-engineered mess, pause and ponder how to make
   it more elegant.

5. If a code improvement requires design input, prompt the human. If you were triggered by cron, you have to complete whichever
   job you choose without human input, so skip tasks that aren't no-brainers.

6. If you find an improvement: make changes, run tests and linting, commit with
   a message prefixed with `[code improver]`.

7. Do not feel obligated to make an edit. If the code is already good, just commit:
   `git commit --allow-empty -m "[code improver] no improvements needed"`
   and stop.
