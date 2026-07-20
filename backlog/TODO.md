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

- migrate this planning / backlog system to Linear app.
- **Deputy agent for worktree contributions** — Ephemeral agent that inspects commits from worktrees before they enter main. Reviews for malicious code, security issues, and project conventions. Spawned by `host_sync_worktree()` before the merge step.
- **Automated repo token refresh via GitHub App** — Replace manually-created fine-grained PATs with a GitHub App that auto-generates short-lived, repo-scoped installation tokens. Eliminates manual rotation. Builds on repo-scoped tokens (Phase 1 complete).
- [Reintroduce Teams with session isolation](0-proposed/reintroduce-teams-session-isolation.md) — Teams tools (`TeamCreate`/`SendMessage`) are unlisted to prevent transcript branching; re-enabling needs per-teammate session isolation first.
- [Split config models schema](0-proposed/split-config-models-schema.md) — Break up `src/pynchy/config/models.py` so small core config additions no longer need a file-length exemption.

### 1 - Approved
*Approved ideas. No plan yet.*

- [Cop-gated learned skill vetting](1-approved/cop-skill-vetting.md) — Quarantine, inspect, and hash-pin learned skills before activation; preserve the Cop and workspace policy as separate security boundaries.
- [Voice transcription](1-approved/voice-transcription.md) — Transcribe inbound voice notes (WhatsApp, Slack) via Whisper API so agents can read audio messages
- [X integration port](1-approved/x-integration-port.md) — Port the archived TypeScript X/Twitter skill from Nanoclaw to Python plugins
- [Periodic agents ideas](1-approved/periodic-agents-ideas.md) — More periodic agent ideas beyond code-improver (security sweeps, SDK updates, etc.)
- [Project ideas](1-approved/project-ideas.md) — Standalone integration ideas (calendar, voice, Cloudflare, AWS, etc.)
- [Small improvements](1-approved/small-improvements.md) — Remaining: slack-tools migration check
- implement 'handoff' tool calls as well as 'delegate' tool calls. handoff causes current agent to cease to exist; it decides what context to give to the next agent. the delegate tool is a blocking call that spawns a new agent to complete a task before passing it back. in reality, this tool call can abstract away a more complex system, like a deep research agent which has many subagents.
- add support for multiple accounts/subscriptions. allow user to designate different workplaces to different accounts (e.g. corporate claude sub, personal claude sub, etc).
- add a self-documenting hook to make the agent update its docs as it learns new things. it should run cmds and be sure that they work before writing docs (otherwise it's a hypothesis, not documetnation)
- beginners tips. the tips print sometimes after a user sends a message. it has usage instructions and pro tips. plugin authors can optionally define tips for their plugins. there should be a global setting to disalbe tips. on by default.
- admin container feature request workflow — agents that want to edit shared files (e.g. `.claude/` rules) should spawn an admin container with a feature request. The admin container decides whether to implement it. (read-only mount enforcement already done in `mount_security.py`)
- port `.claude/` hookify hooks to built-in harness hooks. Claude hookify is vendor-specific (OpenAI doesn't support it). Migrate existing hook logic into our own hook system.
- if container 1 syncs a change, the host recieves and pushes to the rest of the containers, and one of the container's worktree has a merge conflict, and that container is hibernating, that container ought to be spun up, sent a system message about the failed abortion, and a follow up message telling it to fix the broken rebase. that way, working in one container does not fuck up the work of a hibernating container.
- rename subsystems:
    - Providers (AI models)
    - Runtime (container runtimes)
- enforce `"forbidden"` trust level — `TrustLevel = "forbidden"` is declared in `ServiceTrustConfig` but doesn't block anything yet. Needs: (1) SecurityPolicy.evaluate_read/evaluate_write to reject forbidden operations, (2) plugin hook so plugins can declare forbidden operations in their trust config, (3) expose as first-class plugin API so marking a service property as `"forbidden"` actually changes behavior at the plugin level.
- [Google Workspace integration via gog](1-approved/gog-google-workspace-integration.md) — Gmail, Contacts, Docs, and Sheets through typed host-side tools; do not expose unrestricted gog access.
- [Sherpa ONNX text-to-speech plugin](1-approved/sherpa-onnx-tts-plugin.md) — Local speech synthesis plus the outbound-media path needed to deliver audio to channels.
- [Managed flow model on Temporal](1-approved/managed-flow-model.md) — Product-level, durable wait/resume/child-flow state layered on Temporal rather than a Temporal replacement.
- [Document attachment extraction](1-approved/document-attachment-extraction.md) — Safely extract bounded text and fallback page images from inbound document attachments.
- [Tool-result reduction](1-approved/tool-result-reduction.md) — Add an opt-in, core-neutral reducer for safe noisy tool output while preserving raw evidence.
- if a deployment fails, it should spawn a local claude agent out-of-band to rescue the deployment


### 2 - Planning
*Draft plan exists. Awaiting human sign-off.*

- [Plugin: Runtime](2-planning/plugin-runtime.md) — Alternative container runtimes (Apple Container, Podman) as plugins
- [Plugin: Hook](2-planning/plugin-hook.md) — Agent lifecycle hooks provided by plugins (partially superseded by AgentCore refactor — hook abstraction exists in `hooks.py`, remaining work is plugin-provided hook mounting)
  - [Hook Step 1: Base Class](2-planning/plugin-hook-1-base-class.md) — HookPlugin base class and discovery integration
  - [Hook Step 2: Container Input](2-planning/plugin-hook-2-container-input.md) — Extend ContainerInput to carry hook configs
  - [Hook Step 3: Mount Sources](2-planning/plugin-hook-3-mount-sources.md) — Collect configs and mount plugin sources
  - [Hook Step 4: Agent Runner](2-planning/plugin-hook-4-agent-runner.md) — Load and register hooks in container
  - [Hook Step 5: Polish](2-planning/plugin-hook-5-polish.md) — Error handling, docs, and example plugin
- [Reliable bidirectional channel messaging](2-planning/reliable-channel-messaging.md) — Per-channel bidirectional cursors, standardized `Reconcilable` protocol on all channels, outbound ledger with retry, atomic cursor persistence
- [OpenClaw comparison](2-planning/comparison-to-openclaw.md) — Capability, external-action, delegation, and operator-control-plane contracts worth adapting without copying a host-first gateway.
- [NanoClaw comparison](2-planning/comparison-to-nanoclaw.md) — Host-action, attachment/outbox, session-topology, handoff, and setup-plan contracts for Pynchy's container-first architecture.
- [Hermes Agent comparison](2-planning/comparison-to-hermes-agent.md) — Resolved capabilities, channel/media contracts, durable handoffs, conversation navigation, and correlated operational evidence.
- [ZeroClaw comparison](2-planning/comparison-to-zeroclaw.md) — Fail-closed control-plane access, capability truth, operator diagnostics, media, attribution, and replay priorities.
- [NemoClaw comparison](2-planning/comparison-to-nemoclaw.md) — Execution-substrate policy, reproducible image, capability manifests, and evidence-backed diagnostics.
- [ClawHub self-improvement comparison](2-planning/comparison-to-clawhub-self-improvement.md) — Candidate ledger, promotion governance, immutable skills, and host-verified code-improver publication.

### 3 - Ready
*Plan approved or not needed. Ready for an agent to pick up.*

- add a mcp that allows admin accounts to add passwords to the .env. it should be upsert only permissions. it should be written in such a way that when the user pastes in their password, it bypasses the llm, the mcp updates .env, and a message posted on the chat saying they can delete the message they posted containing their password. the password should never be stored in the sqlite db. when using the service adder mcp, it should print a system message saying that if the mcp requires any password_env fields, to paste it in. maybe there should be a magic phrase, like 'env add KEY=VALUE' and this is what the harness intercepts. i guess in that case it shouldn't even be an MCP, it should be part of the harness. oh, and afterwards there should be a system (not host) message broadcast so that the local agent is aware when the env files are updated.

- Observability gaps — Slack message loss alerting and boot failure notifications still open (scheduled task progress heartbeats already resolved)

#### Bugs
- [Slack shutdown race (recurrence)](3-ready/slack-shutdown-race.md) — `RuntimeError: Executor shutdown` during service restart. Commit `76065e0` cancels `_reconnect_task` in `disconnect()`, but orphaned aiohttp subtasks spawned by `connect()` still crash when the executor tears down. Follow-up commit `730e2a7` (guard reconnect against shutdown race) didn't fully resolve it either. Downstream: `Failed to resolve bot user ID (mention stripping disabled)` during reconnect. Needs deeper fix in `slack.py` reconnect path.
#### Docs updates
- we've iterated on our plugin system but havent updated the docs of all the individual plugins to keep them up to date
- we need to improve the docs on the plugins so that it says a bit about pynchy and links back to the main pynchy repo.
- document GDrive MCP setup: google-setup plugin usage (`setup_gdrive`/`enable_gdrive_api`/`authorize_gdrive` service handlers), config.toml `[mcp_servers.gdrive]` reference


### 4 - In Progress
*Being implemented.*

- [Discord General voice workspace](4-in-progress/discord-general-voice.md) — Bind one existing, profile-configured Discord `General` voice channel to Pynchy


### Completed
Completed items do not need an entry here. Preserve a long-form plan or design in
`5-completed/` only when it remains useful as implementation context; otherwise,
remove the plan and rely on git history.

### Denied
We don't track denied items here. Plans are moved to `denied/` via `git mv` and the line is removed.
