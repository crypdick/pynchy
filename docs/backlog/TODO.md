# Backlog

Single source of truth for all pynchy work items.

## Instructions

- Each item is a one-line description linking to its plan file in the matching status folder.
- When adding a new idea, create a stub plan file in the appropriate folder and add a line here.
- Human ideas go straight to `1-approved/`. Agent ideas go to `0-proposed/`.
- When status changes, `git mv` the plan file to the new folder and move the line to the matching section below.
- When denying an item, `git mv` it to `denied/` and remove the line from this file.
- Keep this file clean. One line per item. Link to the plan for details.

## Pipeline

### 0 - Proposed
*ideas awaiting human review - to be discussed.*

- [Plugin verifier agent](0-proposed/plugin-verifier.md) — Automated security audit for third-party plugins before activation (container-based, LLM-powered, SHA-pinned)
- convert setup into pyinfra deployments for repeatable deployments.

### 1 - Approved
*Approved ideas. No plan yet.*

- [Provider-agnostic agents](1-approved/provider-agnostic-agents.md) — Generic agent interface so people can swap in other LLMs
- [X integration port](1-approved/x-integration-port.md) — Port the archived TypeScript X/Twitter skill to Python plugins
- [Periodic agents](1-approved/periodic-agents.md) — Background agents for security sweeps, code quality, SDK updates, etc.
- [Project ideas](1-approved/project-ideas.md) — Standalone integration ideas (calendar, voice, Cloudflare, AWS, etc.)
- [Small improvements](1-approved/small-improvements.md) — Distinct system messages, external pull & restart, dossier logging, ruff hooks, slack-tools check
- [Ray resource orchestration](1-approved/ray-resource-orchestration.md) — Thin Ray integration for resource-aware container scaling, blocking queues, multi-node distribution, and GPU routing
- implement 'handoff' tool calls as well as 'delegate' tool calls. handoff causes current agent to cease to exist; it decides what context to give to the next agent. the delegate tool is a blocking call that spawns a new agent to complete a task before passing it back. in reality, this tool call can abstract away a more complex system, like a deep research agent which has many subagents.
- add support for multiple accounts/subscriptions. allow user to designate different workplaces to different accounts (e.g. corporate claude sub, personal claude sub, etc).
- add a self-documenting hook to make the agent update its docs as it learns new things. it should run cmds and be sure that they work before writing docs (otherwise it's a hypothesis, not documetnation)
- migrate away from single 'god' CLAUDE.md files to .claude/ folders that use the progressive disclosure principle: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices. similarly, some of the docs in the docs/ folder should be migrated to .claude/ files, to keep all claude instructions in a single place
- **[LOW PRIORITY]** Extract Apple Container runtime into standalone plugin (`pynchy-plugin-apple-container`). Requires plugin discovery system first. Serves as reference implementation for RuntimePlugin.
- **[LOW PRIORITY]** Extract agent-browser skill into standalone plugin. Consider if container image size becomes an issue.
- beginners tips. the tips print sometimes after a user sends a message. it has usage instructions and pro tips. plugin authors can optionally define tips for their plugins. there should be a global setting to disalbe tips. on by default.

### 2 - Planning
*Draft plan exists. Awaiting human sign-off.*

- [System message logging](2-planning/system-message-logging.md) — Log actual LLM system prompts to the DB, now that "host" messages have their own sender
- [Plugin: Runtime](2-planning/plugin-runtime.md) — Alternative container runtimes (Apple Container, Podman) as plugins
- [Plugin: Hook](2-planning/plugin-hook.md) — Agent lifecycle hooks provided by plugins (overview - see sub-plans below)
  - [Hook Step 1: Base Class](2-planning/plugin-hook-1-base-class.md) — HookPlugin base class and discovery integration
  - [Hook Step 2: Container Input](2-planning/plugin-hook-2-container-input.md) — Extend ContainerInput to carry hook configs
  - [Hook Step 3: Mount Sources](2-planning/plugin-hook-3-mount-sources.md) — Collect configs and mount plugin sources
  - [Hook Step 4: Agent Runner](2-planning/plugin-hook-4-agent-runner.md) — Load and register hooks in container
  - [Hook Step 5: Polish](2-planning/plugin-hook-5-polish.md) — Error handling, docs, and example plugin

### 3 - Ready
*Plan approved or not needed. Ready for an agent to pick up.*

- [Security hardening](3-ready/security-hardening.md) — Security improvements and hardening measures

### 4 - In Progress
*Being implemented.*

### Completed
We don't track completed items here. Plans are moved to `5-completed/` via `git mv` and the line is removed.

### Denied
We don't track denied items here. Plans are moved to `denied/` via `git mv` and the line is removed.
