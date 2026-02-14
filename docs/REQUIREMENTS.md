# Pynchy Requirements

Original requirements and design decisions from the project creator.

---

## Why This Exists

This is a lightweight, secure alternative to OpenClaw (formerly ClawBot). That project became a monstrosity - 4-5 different processes running different gateways, endless configuration files, endless integrations. It's a security nightmare where agents don't run in isolated processes; there's all kinds of leaky workarounds trying to prevent them from accessing parts of the system they shouldn't. It's impossible for anyone to realistically understand the whole codebase. When you run it you're kind of just yoloing it.

Pynchy gives you the core functionality without that mess.

---

## Philosophy

### Prefer symplicity

The entire codebase should be something you can read and understand. A handful of source files. No microservices, no message queues, no abstraction layers.

### Security Through True Isolation

Instead of application-level permission systems trying to prevent agents from accessing things, agents run in actual Linux containers (Apple Container). The isolation is at the OS level. Agents can only see what's explicitly mounted. Bash access is safe because commands run inside the container, not on your Mac.

### Built for One User

This isn't a framework or a platform. It's working software for my specific needs. I use WhatsApp and Email, so it supports WhatsApp and Email. I don't use Telegram, so it doesn't support Telegram. I add the integrations I actually want, not every possible integration.


### AI-Native Development

I don't need an installation wizard - Claude Code guides the setup. I don't need a monitoring dashboard - I ask Claude Code what's happening. I don't need elaborate logging UIs - I ask Claude to read the logs. I don't need debugging tools - I describe the problem and Claude fixes it.

The codebase assumes you have an AI collaborator. It doesn't need to be excessively self-documenting or self-debugging because Claude is always there.

### Plugins Over Features

When people contribute, they shouldn't add "Telegram support alongside WhatsApp." They should write a plugins and keep this repo minimal.

---

## Vision

A personal Claude assistant accessible via WhatsApp, with minimal custom code.

**Core components:**
- **Claude Agent SDK** as the core agent
- **Persistent memory** per conversation and globally
- **Scheduled tasks** that run Claude and can message back
- **Web access** for search and browsing
- **Browser automation** via agent-browser

**Implementation approach:**
- Use existing tools (WhatsApp connector, Claude Agent SDK, MCP servers)
- Minimal glue code
- File-based systems where possible (CLAUDE.md for memory, folders for groups)

---

## Architecture Decisions

### Transparent Token Stream

The chat history should be a faithful representation of the LLM's token stream. A user reading the conversation should be able to reconstruct the exact contents of the LLM context. Nothing is hidden; every message type is visible and distinguishable.

The Claude Agent SDK message types ([docs](https://platform.claude.com/docs/en/agent-sdk/python#message-types)):

| SDK Type | Description |
|----------|-------------|
| `UserMessage` | User input content |
| `AssistantMessage` | Claude's response — text, thinking, tool use, and tool result blocks |
| `SystemMessage` | System message with metadata (`subtype` + `data` dict) |
| `ResultMessage` | Final result with cost, usage, session_id |

Pynchy should log all of these, plus its own host-process messages, to the DB. The sender vocabulary:

| `sender` value | Visible to LLM? | Description |
|----------------|-----------------|-------------|
| `system` | Yes | Harness-to-model messages — a conversation turn the user can also read |
| `host` | No | Pynchy process notifications (boot, deploy, errors) — user-only |
| `bot` | Yes | Claude's responses (`AssistantMessage`) |
| `deploy` | Yes | Deploy continuation markers |
| `tui-user` | Yes | Messages from the TUI client (`UserMessage`) |
| `{phone_jid}` | Yes | WhatsApp user messages (`UserMessage`) |

The goal: if something went wrong, you can reconstruct what the LLM saw by reading the chat.

### Message Routing
- All channels send messages to the same code path.
- Only messages from registered groups are processed
- Trigger: `@Pynchy` prefix (case insensitive), configurable via `ASSISTANT_NAME` env var
- Unregistered groups are ignored completely
- All channels are kept in sync. Ongoing conversations can be continued from different channels, and all 
  channels display the exact same message history.

### Memory System
- **Per-group memory**: Each group has a folder with its own `CLAUDE.md` and `.claude`.
- **Global memory**: Root `CLAUDE.md` and `.claude/` is read by all groups, but only writable from "god channel" (self-chat).
- If agent wants to edit global memory, it has to send the request to the god container. It decides whether to approve the request.
- **Files**: Groups can create/read files in their folder and reference them
- Agent runs in the group's folder, automatically inherits both CLAUDE.md files

### Session Management
- Each group maintains a conversation session (via Claude Agent SDK)
- Sessions auto-compact when context gets too long, preserving critical information

### Container Isolation
- All agents run inside containers — Apple Container (macOS, preferred) or Docker (macOS/Linux)
- Each agent invocation spawns a container with mounted directories
- Containers provide filesystem isolation - agents can only see mounted paths
- Bash access is safe because commands run inside the container, not on the host
- Browser automation via agent-browser with Chromium in the container (if enabled)
- Avoid the 

### Scheduled Tasks
- Users can ask Claude to schedule recurring or one-time tasks from any group
- Tasks run as full agents in the context of the group that created them
- Tasks have access to all tools including Bash (safe in container)
- Tasks can optionally send messages to their group via `send_message` tool, or complete silently
- Task runs are logged to the database with duration and result
- Schedule types: cron expressions, intervals (ms), or one-time (ISO timestamp)
- From god container: can schedule tasks for any group, view/manage all tasks
- From other groups: can only manage that group's tasks

### Group Management
- New groups are added explicitly via the god channel
- Groups are registered in SQLite (via the god channel or IPC `register_group` command)
- Each group gets a dedicated folder under `groups/`
- Groups can have additional directories mounted via `containerConfig`

### Coordinated Git Sync

Agents inside containers never push to main directly. The host mediates all merges into main, pushes to origin, and syncs other running agents. Design principles:

1. **Prefer mountable files over generated code** — Hook config and scripts live in `container/` as static files, mounted read-only. Don't generate complex logic in Python when a mountable file would do.
2. **Clear host/container naming** — Host-side functions prefixed `host_` (e.g., `host_sync_worktree()`). Container-side scripts live in `container/scripts/`.
3. **Self-contained error messages to containers** — Containers can't read host state (logs, config, etc.). Errors sent to containers must be descriptive and actionable. On conflict, leave the worktree in a resolvable state (conflict markers visible to agent) rather than aborting.
4. **Host owns main** — Agents never push to main directly. The host mediates all merges into main, pushes to origin, and syncs other agents.

### God Channel Privileges
- God channel is the admin/control group (typically self-chat)
- Can write to global memory (`groups/CLAUDE.md`)
- Can schedule tasks for any group
- Can view and manage tasks from all groups
- Can configure additional directory mounts for any group
- Can recieve requests from agents; it decides whether to honor the request. This is mediated by a Deputy agent which blocks malicious requests.

---

## Integration Points

### WhatsApp
- Using baileys library for WhatsApp Web connection
- Messages stored in SQLite, polled by router
- QR code authentication during setup

### Scheduler
- Built-in scheduler runs on the host, spawns containers for task execution
- Custom `pynchy` MCP server (inside container) provides scheduling tools
- Tools: `schedule_task`, `list_tasks`, `pause_task`, `resume_task`, `cancel_task`, `send_message`
- Tasks stored in SQLite with run history
- Scheduler loop checks for due tasks every minute
- Tasks execute Claude Agent SDK in containerized group context

### Web Access
- Built-in WebSearch and WebFetch tools
- Standard Claude Agent SDK capabilities

### Browser Automation
- agent-browser CLI with Chromium in container
- Snapshot-based interaction with element references (@e1, @e2, etc.)
- Screenshots, PDFs, video recording
- Authentication state persistence

---

## Setup & Customization

### Philosophy
- Minimal configuration files
- Setup and customization done via Claude Code
- Users clone the repo and run Claude Code to configure
- Each user gets a custom setup matching their exact needs
- Each user should fork the main repo and use that fork for their deployment. They can ask claude to use the `gh` cli tool to make it private, if they wish.

### Skills
- `/setup` - Install dependencies, authenticate WhatsApp, configure scheduler, start services
- `/customize` - General-purpose skill for adding capabilities (new channels like Telegram, new integrations, behavior changes)

### Deployment
- Runs on local Mac via launchd
- Single Node.js process handles everything

---

## Personal Configuration (Reference)

These are the creator's settings, stored here for reference:

- **Trigger**: `@Pynchy` (case insensitive)
- **Response prefix**: `Pynchy:`
- **Persona**: Default Claude (no custom personality)
- **God channel**: Self-chat (messaging yourself in WhatsApp)

---

## Project Name

**Pynchy** - A reference to Clawdbot (now OpenClaw).
