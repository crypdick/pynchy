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
- right now, each workspace is created using bespoke code. we should ideally have them all configured using a dataclass or similar, so that we can standardize workspaces a bit and enable templating
- beginners tips. the tips print sometimes after a user sends a message. it has usage instructions and pro tips. plugin authors can optionally define tips for their plugins. there should be a global setting to disalbe tips. on by default.
- we should have a design principle that no files from the host get mounted into any containers with write access. if an agent wants to edit a shared file (say, a .claude/ rule) then we should have a way for agents to spawn a new 'god' container with a feature request. the god container decides whether to implement it or not.
- we need a mechanism for spinning down containers. if the worktree is in sync with main, and there hasn't been activity in 10 minutes, the agent container gets killed. this prevents the sync workflow from sending system messages to an inactive container, causing the agent to passively burn tokens for no reason. similarly, deploy shouldn't redeploy the individual containers if they are killed; only active containers.
- daily cron job that redeploys containers with a full container rebuild. make sure that the deploy script does not spin up containers if they are idle.
- a new 'end session' magic word that spins down the container. it runs sync before container is stopped.
- MCPs are known to burn lots of tokens. see whether it's feasible to migrate all MCPs to tools that are passed by the claude sdk. the key requirement is that they execute host-side, or that they have a special channel that can poke an endpoint on the host side that triggers a workflow. these can't be arbitrary code execution, just trigger a workflow.


### 2 - Planning
*Draft plan exists. Awaiting human sign-off.*

- [Plugin: Runtime](2-planning/plugin-runtime.md) — Alternative container runtimes (Apple Container, Podman) as plugins
- [Plugin: Hook](2-planning/plugin-hook.md) — Agent lifecycle hooks provided by plugins (overview - see sub-plans below)
  - [Hook Step 1: Base Class](2-planning/plugin-hook-1-base-class.md) — HookPlugin base class and discovery integration
  - [Hook Step 2: Container Input](2-planning/plugin-hook-2-container-input.md) — Extend ContainerInput to carry hook configs
  - [Hook Step 3: Mount Sources](2-planning/plugin-hook-3-mount-sources.md) — Collect configs and mount plugin sources
  - [Hook Step 4: Agent Runner](2-planning/plugin-hook-4-agent-runner.md) — Load and register hooks in container
  - [Hook Step 5: Polish](2-planning/plugin-hook-5-polish.md) — Error handling, docs, and example plugin
- [Security Hardening](2-planning/security-hardening.md) — Security improvements and hardening measures (overview - see sub-plans below)
  - [Security Step 1: Profiles](2-planning/security-hardening-1-profiles.md) — Workspace security profiles and config schema
  - [Security Step 2: MCP & Policy](2-planning/security-hardening-2-mcp-policy.md) — New MCP tools and basic policy enforcement
  - [Security Step 3: Email](2-planning/security-hardening-3-email.md) — Email service integration (IMAP/SMTP)
  - [Security Step 4: Calendar](2-planning/security-hardening-4-calendar.md) — Calendar service integration (CalDAV)
  - [Security Step 5: Passwords](2-planning/security-hardening-5-passwords.md) — Password manager integration (1Password CLI)
  - [Security Step 6: Approval](2-planning/security-hardening-6-approval.md) — Human approval gate for high-risk actions
  - [Security Step 7: Input Filter](2-planning/security-hardening-7-input-filter.md) — Deputy Agent for prompt injection detection (optional)

### 3 - Ready
*Plan approved or not needed. Ready for an agent to pick up.*


- rename the 'main container' to the 'God container' in code and docs. this is to disambiguate from 'main' branch.

#### Bugs
- messaging is broken. when I send a message, sometimes I see no response in the chat. then when i send a follow up message, it responds to the previous message. the system is desynchronized somehow. update: the message the agents send (as well as tool calls, other messages) seem to be sending to whatsapp more reliably than the tui.
- previously, the messages sent to the channels were prefixes by emoticons to denote the sender (system, bot, tool call, tool response, host). theyve been reverted to text prefixes like [system]
- we need to improve the robustness of synchronization. deploys keep failing because the remote does not push their local commits.
- when I woke up today, pynchy had created a new Pynchy whatsapp group instead of using the existing one. fix this bug.


### 4 - In Progress
*Being implemented.*

### Completed
We don't track completed items here. Plans are moved to `5-completed/` via `git mv` and the line is removed.

### Denied
We don't track denied items here. Plans are moved to `denied/` via `git mv` and the line is removed.
