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

### 1 - Approved
*Approved ideas. No plan yet.*

- [Provider-agnostic agents](1-approved/provider-agnostic-agents.md) — Generic agent interface so people can swap in other LLMs
- [X integration port](1-approved/x-integration-port.md) — Port the archived TypeScript X/Twitter skill to Python plugins
- [Periodic agents](1-approved/periodic-agents.md) — Background agents for security sweeps, code quality, SDK updates, etc.
- [Project ideas](1-approved/project-ideas.md) — Standalone integration ideas (calendar, voice, Cloudflare, AWS, etc.)
- [Small improvements](1-approved/small-improvements.md) — Distinct system messages, external pull & restart, dossier logging, ruff hooks, slack-tools check
- [Ray resource orchestration](1-approved/ray-resource-orchestration.md) — Thin Ray integration for resource-aware container scaling, blocking queues, multi-node distribution, and GPU routing

### 2 - Planning
*Draft plan exists. Awaiting human sign-off.*

(none)

### 3 - Ready
*Plan approved. Ready for an agent to pick up.*

- [Plugin system](3-ready/plugin-system.md) — Plugin architecture for extending pynchy with modular capabilities
- [Security hardening](3-ready/security-hardening.md) — Security improvements and hardening measures
- allow user to execute cmds, bypassing the llm. `!ls`, `!echo hi`, will run without llm approval. the llm will see in the history that the user ran a cmd and the cmd output. this type of user input does not initiate a conversation turn for the llm though-- the user needs to follow up with a non-command message in order to trigger the llm (at which point it'll see the tool usage in its history) 
- implement 'handoff' tool calls as well as 'delegate' tool calls. handoff causes current agent to cease to exist; it decides what context to give to the next agent. the delegate tool is a blocking call that spawns a new agent to complete a task before passing it back. in reality, this tool call can abstract away a more complex system, like a deep research agent which has many subagents.
- add support for multiple accounts/subscriptions. allow user to designate different workplaces to different accounts (e.g. corporate claude sub, personal claude sub, etc). 
- add emoji to messages when they've been read by the agent. whatsapp should also send a '...' placeholder message while the agent is working on a response. errors should also be propagated into whatsapp as system messages.
- add a self-documenting hook to make the agent update its docs as it learns new things. it should run cmds and be sure that they work before writing docs (otherwise it's a hypothesis, not documetnation)

### 4 - In Progress
*Being implemented.*

(none)

### Completed
We don't track completed items here. Plans are moved to `5-completed/` via `git mv` and the line is removed.

### Denied
We don't track denied items here. Plans are moved to `denied/` via `git mv` and the line is removed.
