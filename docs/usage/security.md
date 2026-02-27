# Service Trust

This page explains how to configure trust declarations for services that agents access. These declarations control when Pynchy requires human approval before an agent acts — protecting against prompt injection attacks that try to exfiltrate sensitive data.

## The Problem: The Lethal Trifecta

An agent becomes dangerous when it has all three of:

- **Untrusted input** — data from sources you don't control (emails from strangers, Slack messages, web pages)
- **Sensitive data** — information that would cause harm if leaked (corporate docs, credentials, private conversations)
- **Untrusted output** — channels that reach the outside world (sending emails, posting messages, submitting forms)

Any *two* of these is manageable. All three together means a prompt injection attack in an incoming message could trick the agent into leaking sensitive data through an outbound channel.

## Four Properties Per Service

Each service declares four trust properties in `config.toml`:

```toml
[services.slack_mcp_acme]
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

**Unknown services default to all-true** (maximum gating). Declare a service to loosen its policy.

## How Gating Works

When an agent reads from a service, Pynchy tracks two *taint flags*:

- **Corruption taint** — set when the agent reads from a `public_source`. Stays set for the entire session.
- **Secret taint** — set when the agent reads `secret_data` or accesses a workspace marked `contains_secrets = true`.

When the agent writes to a service, the gating matrix applies:

```
Write to service
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

A payload scanner also runs on all outbound writes. If it detects credential patterns (API keys, tokens, passwords), the write escalates to human approval regardless of taint state.

## Configuration Examples

### Personal calendar (fully trusted)

Your own Nextcloud calendar — you control the data, events aren't secrets, writing to it is safe.

```toml
[services.caldav]
public_source = false
secret_data = false
public_sink = false
dangerous_writes = false
```

Result: no gating. Agents read and write freely.

### Web browser (fully untrusted)

Browses the open web — classic untrusted source and sink.

```toml
[services.playwright]
public_source = true
secret_data = false
public_sink = true
dangerous_writes = true
```

Result: reading web content taints the agent. Any subsequent write to a public sink or dangerous service requires approval.

### Corporate Slack (sensitive + untrusted)

Messages from coworkers — generally trusted people, but still external input. Corporate conversations contain sensitive information.

```toml
[services.slack_mcp_acme]
public_source = true
secret_data = true
public_sink = true
dangerous_writes = true
```

Result: full gating. Reading messages sets both taint flags. Sending messages requires human approval (the lethal trifecta: untrusted input + sensitive data + untrusted output).

### Corporate Google Drive (sensitive but controlled)

Your organization's Drive — you control what's in it, but the contents are confidential.

```toml
[services.gdrive]
public_source = false
secret_data = true
public_sink = false
dangerous_writes = false
```

Result: reading Drive files sets the secret taint but not the corruption taint. Writes to Drive are ungated. However, if the agent *also* read from an untrusted source (like a Slack message or web page), then writing to a public sink would require approval — because both taints are now set.

## Per-Workspace Overrides

Mark workspaces that contain sensitive information:

```toml
[sandbox.acme-1.security]
contains_secrets = true
```

When an agent accesses a workspace with `contains_secrets = true`, the secret taint flag gets set. This means any agent working in a corporate workspace that also reads from an untrusted source will trigger approval gates on outbound writes.

## Admin Clean Room

Admin workspaces are protected by a **clean room policy**: they cannot have any MCP server with `public_source=true`. This is enforced at startup — Pynchy refuses to start if an admin workspace references a public-source MCP.

This means the admin workspace can never become corruption-tainted (it never reads untrusted content), which eliminates prompt injection as a threat vector for the most privileged operations.

If an MCP server is not declared in `[services]`, it defaults to `public_source=true` (maximally cautious). To use an MCP in an admin workspace, you must explicitly declare it with `public_source = false`.

**Example error:**
```
Admin workspace 'admin-1' has MCP server 'playwright' with public_source=True.
Admin workspaces cannot have public_source MCPs (clean room policy).
```

For web browsing, email, or other untrusted-input tasks, use a non-admin workspace.

## Bash Command Gating

Agents have access to a general-purpose Bash tool. The bash security gate inspects every command before it runs, using the same taint tracking as the service trust policy above.

**Safe commands always execute.** Common development tools — `ls`, `cat`, `grep`, `sed`, `jq`, `find`, `git`, `wc`, and dozens more — are on a local whitelist. These cannot reach the network and run without any delay or IPC.

**Network commands are gated when tainted.** Commands like `curl`, `wget`, `python`, `ssh`, `pip install`, and similar network-capable tools are evaluated against the session's taint state:

- **No taint** — the command runs. There is no sensitive data to exfiltrate.
- **Corruption tainted only** — the Cop (LLM-based inspector) reviews the command. If the Cop flags it, the command is denied.
- **Both corruption and secret tainted** — the command requires human approval before executing, just like the lethal trifecta gate for service writes.

**Unknown commands get Cop review.** Commands not on either the safe or network list are sent to the Cop for inspection. If the Cop flags the command and both taint flags are set, the decision escalates to human approval.

No configuration is needed — the bash security gate is always active. For technical details, see [Bash Security Gate](../architecture/security.md#5a-bash-security-gate).

## Host-Mutating Operations

Certain IPC operations can change what code runs on the host: merging code, registering new workspaces, scheduling tasks, and running host commands. These are automatically inspected by the **Cop** — an LLM-based security reviewer.

The Cop examines the payload of each host-mutating operation (the diff being merged, the task prompt, the group config) and flags anything suspicious. If flagged, the operation requires human approval before proceeding.

**What's covered:**
- Code merges (`sync_worktree_to_main`)
- Workspace registration (`register_group`)
- Periodic agent creation (`create_periodic_agent`)
- Task scheduling (`schedule_task`, `schedule_host_job`)
- Script-type MCP tool calls (auto-classified — any MCP with `type = "script"`)

**What's not covered:** Docker-type MCPs (isolated in their own container), URL-type MCPs (remote, no host access), and deploy (just restarts with existing code).

No configuration needed — host-mutating inspection is always on.

## Choosing Values

Ask these questions for each service:

1. **public_source** — "Can strangers put content into this service that my agent will read?" Slack messages from external parties: yes. Your personal calendar: no.
2. **secret_data** — "Would I regret it if this data leaked publicly?" Corporate Slack history: yes. A public-facing calendar: no.
3. **public_sink** — "Can this service send data to people outside my control?" Email, Slack DMs, web forms: yes. Writing to your own Drive: no.
4. **dangerous_writes** — "Is a write irreversible or high-impact?" Sending a message: yes. Editing a calendar event: no.

When in doubt, leave a property as `true` — the default is maximum gating. You can always loosen later.

---

**Want to customize this?** The trust model is built into Pynchy core. For details on how gating decisions are enforced, see [Security Architecture](../architecture/security.md#5-service-trust-policy-lethal-trifecta-defenses).
