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
*Agent-generated ideas awaiting human review.*

(none)

### 1 - Approved
*Approved ideas. No plan yet.*

- [Provider-agnostic agents](1-approved/provider-agnostic-agents.md) — Generic agent interface so people can swap in other LLMs
- [Tailscale integration](1-approved/tailscale-integration.md) — Remote access, deploys, health checks, CLI interaction
- [X integration port](1-approved/x-integration-port.md) — Port the archived TypeScript X/Twitter skill to Python plugins
- [Periodic agents](1-approved/periodic-agents.md) — Background agents for security sweeps, code quality, SDK updates, etc.
- [Project ideas](1-approved/project-ideas.md) — Standalone integration ideas (calendar, voice, Cloudflare, AWS, etc.)
- [Small improvements](1-approved/small-improvements.md) — WhatsApp context reset, dossier logging, ruff hooks, slack-tools check

### 2 - Planning
*Draft plan exists. Awaiting human sign-off.*

(none)

### 3 - Ready
*Plan approved. Ready for an agent to pick up.*

- [Plugin system](3-ready/plugin-system.md) — Plugin architecture for extending pynchy with modular capabilities
- [Security hardening](3-ready/security-hardening.md) — Security improvements and hardening measures

### 4 - In Progress
*Being implemented.*

(none)

### Completed
We don't track completed items here. Plans are moved to `5-completed/` via `git mv` and the line is removed.

### Denied
We don't track denied items here. Plans are moved to `denied/` via `git mv` and the line is removed.
