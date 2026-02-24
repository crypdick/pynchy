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
  ├─ corruption + public_sink        →  DEPUTY REVIEW
  │                                     (content scan, future: LLM review)
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

## Choosing Values

Ask these questions for each service:

1. **public_source** — "Can strangers put content into this service that my agent will read?" Slack messages from external parties: yes. Your personal calendar: no.
2. **secret_data** — "Would I regret it if this data leaked publicly?" Corporate Slack history: yes. A public-facing calendar: no.
3. **public_sink** — "Can this service send data to people outside my control?" Email, Slack DMs, web forms: yes. Writing to your own Drive: no.
4. **dangerous_writes** — "Is a write irreversible or high-impact?" Sending a message: yes. Editing a calendar event: no.

When in doubt, leave a property as `true` — the default is maximum gating. You can always loosen later.

---

**Want to customize this?** The trust model is built into Pynchy core. For details on how gating decisions are enforced, see [Security Architecture](../architecture/security.md#5-service-trust-policy-lethal-trifecta-defenses).
