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
- **Deputy agent for worktree contributions** — Ephemeral agent that inspects commits from worktrees before they enter main. Reviews for malicious code, security issues, and project conventions. Spawned by `host_sync_worktree()` before the merge step.

### 1 - Approved
*Approved ideas. No plan yet.*

- [X integration port](1-approved/x-integration-port.md) — Port the archived TypeScript X/Twitter skill to Python plugins
- [Periodic agents ideas](1-approved/periodic-agents.md) — Background agents for security sweeps, SDK updates, etc. (infra done: `task_scheduler.py`; 1 agent live: `code-improver`)
- [Project ideas](1-approved/project-ideas.md) — Standalone integration ideas (calendar, voice, Cloudflare, AWS, etc.)
- [Small improvements](1-approved/small-improvements.md) — Remaining: slack-tools migration check (3/5 done)
- [LiteLLM Gateway](1-approved/litellm-gateway.md) — Host-side LLM proxy for credential isolation, per-group budgets, and provider-agnostic routing. Prerequisite for extracting Claude/OpenAI backends into plugins.
- [Ray resource orchestration](1-approved/ray-resource-orchestration.md) — Thin Ray integration for resource-aware container scaling, blocking queues, multi-node distribution, and GPU routing
- implement 'handoff' tool calls as well as 'delegate' tool calls. handoff causes current agent to cease to exist; it decides what context to give to the next agent. the delegate tool is a blocking call that spawns a new agent to complete a task before passing it back. in reality, this tool call can abstract away a more complex system, like a deep research agent which has many subagents.
- add support for multiple accounts/subscriptions. allow user to designate different workplaces to different accounts (e.g. corporate claude sub, personal claude sub, etc).
- add a self-documenting hook to make the agent update its docs as it learns new things. it should run cmds and be sure that they work before writing docs (otherwise it's a hypothesis, not documetnation)
- **[LOW PRIORITY]** Extract agent-browser skill into standalone plugin. Consider if container image size becomes an issue.
- beginners tips. the tips print sometimes after a user sends a message. it has usage instructions and pro tips. plugin authors can optionally define tips for their plugins. there should be a global setting to disalbe tips. on by default.
- god container feature request workflow — agents that want to edit shared files (e.g. `.claude/` rules) should spawn a god container with a feature request. The god container decides whether to implement it. (read-only mount enforcement already done in `mount_security.py`)
- port `.claude/` hookify hooks to built-in harness hooks. Claude hookify is vendor-specific (OpenAI doesn't support it). Migrate existing hook logic into our own hook system.
- [Plan mode diff view](1-approved/plan-mode-diff-view.md) — Show full plan diff when agent exits plan mode so user can review what changed
- **Rethink DB event cursor design** — The message polling loop uses a single `last_timestamp` cursor shared across all consumers (WhatsApp, TUI, running agents). This means advancing the cursor for one consumer silently advances it for all others, so events can be missed. Each subscriber (each channel, each running agent container) should have its own independent cursor into the DB event stream.
- if container 1 syncs a change, the host recieves and pushes to the rest of the containers, and one of the container's worktree has a merge conflict, and that container is hibernating, that container ought to be spun up, sent a system message about the failed abortion, and a follow up message telling it to fix the broken rebase. that way, working in one container does not fuck up the work of a hibernating container.
- rename subsystems:
  - Providers (AI models)
  - Runtime (container runtimes)


### 2 - Planning
*Draft plan exists. Awaiting human sign-off.*

- [Plugin: Runtime](2-planning/plugin-runtime.md) — Alternative container runtimes (Apple Container, Podman) as plugins
- [Plugin: Hook](2-planning/plugin-hook.md) — Agent lifecycle hooks provided by plugins (partially superseded by AgentCore refactor — hook abstraction exists in `hooks.py`, remaining work is plugin-provided hook mounting)
  - [Hook Step 1: Base Class](2-planning/plugin-hook-1-base-class.md) — HookPlugin base class and discovery integration
  - [Hook Step 2: Container Input](2-planning/plugin-hook-2-container-input.md) — Extend ContainerInput to carry hook configs
  - [Hook Step 3: Mount Sources](2-planning/plugin-hook-3-mount-sources.md) — Collect configs and mount plugin sources
  - [Hook Step 4: Agent Runner](2-planning/plugin-hook-4-agent-runner.md) — Load and register hooks in container
  - [Hook Step 5: Polish](2-planning/plugin-hook-5-polish.md) — Error handling, docs, and example plugin
- [Security Hardening](2-planning/security-hardening.md) — Security improvements and hardening measures (overview - see sub-plans below)
  - [Security Step 0: IPC Surface](2-planning/security-hardening-0-ipc-surface.md) — Reduce IPC to signal-only protocol, Deputy mediation for data-carrying requests, replace polling with inotify
  - [Security Step 1: Profiles](2-planning/security-hardening-1-profiles.md) — Workspace security profiles and config schema (types partially done: `WorkspaceProfile`/`WorkspaceSecurity` in `types.py`)
  - [Security Step 2: MCP & Policy](2-planning/security-hardening-2-mcp-policy.md) — New MCP tools and basic policy enforcement
  - [Security Step 3: Email](2-planning/security-hardening-3-email.md) — Email service integration (IMAP/SMTP)
  - [Security Step 4: Calendar](2-planning/security-hardening-4-calendar.md) — Calendar service integration (CalDAV)
  - [Security Step 5: Passwords](2-planning/security-hardening-5-passwords.md) — Password manager integration (1Password CLI)
  - [Security Step 6: Approval](2-planning/security-hardening-6-approval.md) — Human approval gate for high-risk actions
  - [Security Step 7: Input Filter](2-planning/security-hardening-7-input-filter.md) — Deputy Agent for prompt injection detection (optional)

### 3 - Ready
*Plan approved or not needed. Ready for an agent to pick up.*

- factor out tailscale support into a separate plugin. make sure that at least one tunnel is always active. we might need to create a new tunnel plugin type, and update the cookiecutter template.
- factor out openai backend as a separate plugin
- factor out claude backend as a separate plugin
- make the code improver plugin able to update the plugin repos as well as the core pynchy repo.

#### Bugs
- messaging is broken. when I send a message, sometimes I see no response in the chat. then when i send a follow up message, it responds to the previous message. the system is desynchronized somehow. update: the message the agents send (as well as tool calls, other messages) seem to be sending to whatsapp more reliably than the tui.

#### Docs updates
- we've iterated on our plugin system but havent updated the docs of all the individual plugins to keep them up to date
- we need to improve the docs on the plugins and the and the cookiecutter template so that it says a bit about pynchy and links back to the main pynchy repo.


### 4 - In Progress
*Being implemented.*

### Completed
We don't track completed items here. Plans are moved to `5-completed/` via `git mv` and the line is removed.

### Denied
We don't track denied items here. Plans are moved to `denied/` via `git mv` and the line is removed.
