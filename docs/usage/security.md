# Tool Trust

Configure trust declarations for tools that agents access. These control when Pynchy requires human approval before an agent acts — protecting against prompt injection attacks that try to exfiltrate sensitive data.

## The Problem: The Lethal Trifecta

An agent becomes dangerous when it has all three:

- **Untrusted input** — data from sources you don't control (emails from strangers, Slack messages, web pages)
- **Sensitive data** — information that would cause harm if leaked (corporate docs, credentials, private conversations)
- **Untrusted output** — channels that reach the outside world (sending emails, posting messages, submitting forms)

Any *two* are manageable. All three together means a prompt injection attack in an incoming message can trick the agent into leaking sensitive data through an outbound channel.

## Four Properties Per Tool

<!-- Source of truth: ServiceTrustConfig in src/pynchy/types.py — keep these properties/defaults in sync. -->
Each configured tool declares four trust properties in `config.toml`:

```toml
[tools.slack_mcp_acme]
type = "mcp"
public_source = true        # messages from others — untrusted input
secret_data = true          # corporate conversations — sensitive
public_sink = true          # can send messages — untrusted output
dangerous_writes = true     # sending is irreversible
```

| Property | Question | `false` | `true` | `"forbidden"` |
|----------|----------|---------|--------|---------------|
| `public_source` | Can this deliver content from untrusted parties? | Safe | Taints the agent | Blocked |
| `secret_data` | Would leaking this data cause harm? | Safe | Taints the agent | Blocked |
| `public_sink` | Can this send data to untrusted parties? | Safe | Gated when tainted | Blocked |
| `dangerous_writes` | Are writes irreversible or high-impact? | Safe | Requires approval | Blocked |

Tools default to all-true (maximum gating). Set trust fields to `false` only when that risk does not apply.

## How Gating Works

When an agent reads from a service, Pynchy tracks two *taint flags*:

- **Corruption taint** — set when the agent reads from a `public_source`. Sticks for the rest of the session.
- **Secret taint** — set when the agent reads `secret_data` or accesses a workspace whose profile has `contains_secrets = true`.

When the agent writes to a service, the gating matrix kicks in:

```
Write to tool
  │
  ├─ dangerous_writes = "forbidden"  →  BLOCKED (always)
  │
  ├─ dangerous_writes = true         →  HUMAN APPROVAL REQUIRED
  │
  ├─ corruption + secret + public_sink  →  HUMAN APPROVAL REQUIRED
  │                                        (the lethal trifecta)
  │
  ├─ corruption + public_sink        →  COP REVIEW
  │                                     (LLM-based content scan)
  │
  └─ none of the above              →  ALLOWED
```

A payload scanner also runs on every outbound write. If it spots credential patterns (API keys, tokens, passwords), the write escalates to human approval regardless of taint state.

## Capability Policy

Use capability rules for semantic tool permissions: block or approve a specific
tool action before it executes. Tool trust still handles taint and exfiltration
risk after the capability gate passes.

MCP tool calls use capability IDs in this form:

```text
mcp.<server-or-instance>.<tool-name>
```

For example, a `send` tool on an `email` MCP server maps to `mcp.email.send`.
Rules accept these decisions:

| Decision | Meaning |
|----------|---------|
| `allow` | Allow the capability and continue to tool trust checks |
| `needs_human` | Ask for human approval before executing |
| `deny` | Block the capability |

Rules support trailing wildcards. Exact rules win over wildcard rules:

```toml
[profiles.research.capabilities."mcp.email.*"]
decision = "needs_human"

[profiles.research.capabilities."mcp.email.preview"]
decision = "allow"

[profiles.readonly.capabilities."mcp.email.send"]
decision = "deny"
```

Capability maps live on reusable profiles. Compose profiles into workspaces to
share policy across related workspaces.

## Configuration Examples

### Personal calendar (fully trusted)

Your own Nextcloud calendar — you own the data, events aren't secrets, writes are safe.

```toml
[tools.caldav]
type = "builtin"
name = "caldav"
public_source = false
secret_data = false
public_sink = false
dangerous_writes = false
```

Result: no gating. Agents read and write freely.

### Web browser (fully untrusted)

Browses the open web — classic untrusted source and sink.

```toml
[tools.playwright]
type = "mcp"
public_source = true
secret_data = false
public_sink = true
dangerous_writes = true

[tools.playwright.mcp]
runtime = "docker"
image = "mcr.microsoft.com/playwright/mcp:latest"
port = 8931
```

Result: reading web content taints the agent. Any later write to a public sink or dangerous service requires approval.

### Corporate Slack (sensitive + untrusted)

Messages from coworkers — generally trusted people, but still external input. Corporate conversations contain sensitive information.

```toml
[tools.slack_mcp_acme]
type = "mcp"
public_source = true
secret_data = true
public_sink = true
dangerous_writes = true

[tools.slack_mcp_acme.mcp]
runtime = "docker"
image = "ghcr.io/korotovsky/slack-mcp-server:latest"
port = 8080
```

Result: full gating. Reading messages sets both taint flags. Sending messages requires human approval (the lethal trifecta: untrusted input + sensitive data + untrusted output).

### Corporate Google Drive (sensitive but controlled)

Your org's Drive — you control what's in it, but the contents are confidential.

```toml
[tools.gdrive]
type = "mcp"
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false

[tools.gdrive.mcp]
runtime = "docker"
image = "pynchy-mcp-gdrive:latest"
port = 3000
```

Result: reading Drive files sets the secret taint but not the corruption taint. Writes to Drive are ungated. But if the agent *also* read from an untrusted source (a Slack message, web page), then writing to a public sink requires approval — both taints are set.

## Profile Secret Classification

Mark profiles whose workspaces contain sensitive information:

```toml
[profiles.acme-worker]
contains_secrets = true
```

Accessing a workspace whose profile has `contains_secrets = true` sets the secret taint flag. Any agent in a corporate workspace that also reads from an untrusted source will hit approval gates on outbound writes.

## Admin Clean Room

Admin workspaces are protected by a **clean room policy**: they cannot select any tool with `public_source=true`. This is enforced at startup — Pynchy refuses to start if an admin workspace resolves to a public-source tool.

This means the admin workspace can never become corruption-tainted (it never reads untrusted content), which eliminates prompt injection as a threat vector for the most privileged operations.

To use an MCP-backed tool in an admin workspace, declare the tool and set `public_source = false`.

**Example error:**
```
Admin workspace 'admin-1' has tool 'playwright' with public_source=True.
Admin workspaces cannot use public-source tools.
```

For web browsing, email, or other untrusted-input tasks, use a non-admin workspace.

## Bash Command Gating

Agents have a general-purpose Bash tool. The bash security gate inspects every command before it runs, using the same taint tracking as the tool trust policy above.

**Safe commands always run.** Common dev tools — `ls`, `cat`, `grep`, `sed`, `jq`, `find`, `git`, `wc`, and dozens more — are on a local whitelist. They can't reach the network and run with no delay or IPC.

**Network commands are gated when tainted.** Commands like `curl`, `wget`, `python`, `ssh`, `pip install`, and similar network-capable tools are checked against the session's taint state:

- **No taint** — the command runs. Nothing sensitive to exfiltrate.
- **Corruption tainted only** — the Cop (LLM-based inspector) reviews the command. If the Cop flags it, the command is denied.
- **Both corruption and secret tainted** — the command requires human approval, same as the lethal trifecta gate for service writes.

**Unknown commands get Cop review.** Commands not on either list go to the Cop for inspection. If the Cop flags the command and both taint flags are set, the decision escalates to human approval.

No config needed — the bash security gate is always active. For technical details, see [Bash Security Gate](../architecture/security.md#5b-bash-security-gate).

## Host-Mutating Operations

Some IPC operations can change what code runs on the host: merging code, registering new workspaces, scheduling tasks, running host commands. These are automatically inspected by the **Cop** — an LLM-based security reviewer.

The Cop examines the payload of each host-mutating operation (the diff being merged, the task prompt, the group config) and flags anything suspicious. Flagged operations require human approval before proceeding.

**What's covered:**
- Code merges (`sync_worktree_to_main`)
- Workspace registration (`register_group`)
- Periodic agent creation (`create_periodic_agent`)
- Task scheduling (`schedule_task`, `schedule_host_job`)
- Script-type MCP tool calls (auto-classified — any MCP with `type = "script"`)

**What's not covered:** Docker-type MCPs (isolated in their own container), URL-type MCPs (remote, no host access), and deploy (just restarts with existing code).

No config needed — host-mutating inspection is always on.

## Choosing Values

For each tool:

1. **public_source** — "Can strangers put content into this tool that my agent will read?" Slack messages from external parties: yes. Your personal calendar: no.
2. **secret_data** — "Would I regret it if this data leaked publicly?" Corporate Slack history: yes. A public-facing calendar: no.
3. **public_sink** — "Can this tool send data to people outside my control?" Email, Slack DMs, web forms: yes. Writing to your own Drive: no.
4. **dangerous_writes** — "Is a write irreversible or high-impact?" Sending a message: yes. Editing a calendar event: no.

When in doubt, leave a property as `true` — the default is maximum gating. Loosen later.

---

**Want to customize this?** The trust model is built into Pynchy core. For details on how gating decisions are enforced, see [Security Architecture](../architecture/security.md#5-service-trust-policy-lethal-trifecta-defenses).
