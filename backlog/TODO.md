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

- convert setup into pyinfra deployments for repeatable deployments.
- **Deputy agent for worktree contributions** — Ephemeral agent that inspects commits from worktrees before they enter main. Reviews for malicious code, security issues, and project conventions. Spawned by `host_sync_worktree()` before the merge step.
- **Automated repo token refresh via GitHub App** — Replace manually-created fine-grained PATs with a GitHub App that auto-generates short-lived, repo-scoped installation tokens. Eliminates manual rotation. Builds on [repo-scoped tokens](5-completed/repo-scoped-tokens.md) (Phase 1 complete).

### 1 - Approved
*Approved ideas. No plan yet.*

- [Voice transcription](1-approved/voice-transcription.md) — Transcribe inbound voice notes (WhatsApp, Slack) via Whisper API so agents can read audio messages
- [X integration port](1-approved/x-integration-port.md) — Port the archived TypeScript X/Twitter skill to Python plugins
- [Periodic agents ideas](1-approved/periodic-agents-ideas.md) — More periodic agent ideas beyond code-improver (security sweeps, SDK updates, etc.)
- [Project ideas](1-approved/project-ideas.md) — Standalone integration ideas (calendar, voice, Cloudflare, AWS, etc.)
- [Small improvements](1-approved/small-improvements.md) — Remaining: slack-tools migration check
- [Ray resource orchestration](1-approved/ray-resource-orchestration.md) — Thin Ray integration for resource-aware container scaling, blocking queues, multi-node distribution, and GPU routing
- implement 'handoff' tool calls as well as 'delegate' tool calls. handoff causes current agent to cease to exist; it decides what context to give to the next agent. the delegate tool is a blocking call that spawns a new agent to complete a task before passing it back. in reality, this tool call can abstract away a more complex system, like a deep research agent which has many subagents.
- add support for multiple accounts/subscriptions. allow user to designate different workplaces to different accounts (e.g. corporate claude sub, personal claude sub, etc).
- add a self-documenting hook to make the agent update its docs as it learns new things. it should run cmds and be sure that they work before writing docs (otherwise it's a hypothesis, not documetnation)
- beginners tips. the tips print sometimes after a user sends a message. it has usage instructions and pro tips. plugin authors can optionally define tips for their plugins. there should be a global setting to disalbe tips. on by default.
- admin container feature request workflow — agents that want to edit shared files (e.g. `.claude/` rules) should spawn an admin container with a feature request. The admin container decides whether to implement it. (read-only mount enforcement already done in `mount_security.py`)
- port `.claude/` hookify hooks to built-in harness hooks. Claude hookify is vendor-specific (OpenAI doesn't support it). Migrate existing hook logic into our own hook system.
- hide `register_group` tool from non-admin containers — currently the tool definition is always returned (only the handler checks `is_admin`). Should return `None` from `_register_group_definition()` when `_ipc.is_admin` is false, like `deploy_changes` already does.
- if container 1 syncs a change, the host recieves and pushes to the rest of the containers, and one of the container's worktree has a merge conflict, and that container is hibernating, that container ought to be spun up, sent a system message about the failed abortion, and a follow up message telling it to fix the broken rebase. that way, working in one container does not fuck up the work of a hibernating container.
- rename subsystems:
  - Providers (AI models)
  - Runtime (container runtimes)
- GDrive integration
- GMail integration
- Protonmail integration
- migrate to scraping jsonl files


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
- [Reliable bidirectional channel messaging](2-planning/reliable-channel-messaging.md) — Per-channel bidirectional cursors, standardized `Reconcilable` protocol on all channels, outbound ledger with retry, atomic cursor persistence

### 3 - Ready
*Plan approved or not needed. Ready for an agent to pick up.*

- factor out tailscale support into a separate plugin. make sure that at least one tunnel is always active. we might need to create a new tunnel plugin type.
- factor out openai backend as a separate plugin (currently built-in at `plugin/builtin_agent_openai.py` — needs extraction to separate package)
- factor out claude backend as a separate plugin (currently built-in at `plugin/builtin_agent_claude.py` — needs extraction to separate package)
- make the code improver plugin able to update the plugin repos as well as the core pynchy repo.

#### Bugs
- [MCP gateway transport](3-ready/mcp-gateway-transport.md) — Claude SDK `type: "http"` hangs during init against LiteLLM's Streamable HTTP `/mcp/` endpoint; `type: "sse"` fails gracefully but tools unavailable
- [Slack shutdown race (recurrence)](3-ready/slack-shutdown-race.md) — `RuntimeError: Executor shutdown` during service restart. Commit `76065e0` cancels `_reconnect_task` in `disconnect()`, but orphaned aiohttp subtasks spawned by `connect()` still crash when the executor tears down. Follow-up commit `730e2a7` (guard reconnect against shutdown race) didn't fully resolve it either. Downstream: `Failed to resolve bot user ID (mention stripping disabled)` during reconnect. Needs deeper fix in `slack.py` reconnect path.
- messaging desync — sometimes no response appears in TUI until a follow-up message is sent. Partially fixed (cursor advance bug, input pipeline unification), but full fix likely depends on per-channel bidirectional cursors (see [reliable-channel-messaging](2-planning/reliable-channel-messaging.md)).

#### Docs updates
- we've iterated on our plugin system but havent updated the docs of all the individual plugins to keep them up to date
- we need to improve the docs on the plugins so that it says a bit about pynchy and links back to the main pynchy repo.
- document GDrive MCP setup: google-setup plugin usage (`setup_gdrive`/`enable_gdrive_api`/`authorize_gdrive` service handlers), config.toml `[mcp_servers.gdrive]` reference


### 4 - In Progress
*Being implemented.*

- [Status endpoint](4-in-progress/status-endpoint.md) — Comprehensive `GET /status` endpoint aggregating health from all subsystems (service, channels, gateway, queue, repos/worktrees, messages, tasks, host jobs)

### Completed
We don't track completed items here. Plans are moved to `5-completed/` via `git mv` and the line is removed.

### Denied
We don't track denied items here. Plans are moved to `denied/` via `git mv` and the line is removed.
