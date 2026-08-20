# Tool Trust

Configure trust declarations for tools that agents access. These control when Pynchy requires human approval before an agent acts — protecting against prompt injection attacks that try to exfiltrate sensitive data.

## The Problem: The Lethal Trifecta

An agent becomes dangerous when it has all three:

- **Untrusted input** — data from sources you don't control (emails from strangers, Slack messages, web pages)
- **Sensitive data** — information that would cause harm if leaked (corporate docs, credentials, private conversations)
- **Untrusted output** — channels that reach the outside world (sending emails, posting messages, submitting forms)

Any *two* are manageable. All three together means a prompt injection attack in an incoming message can trick the agent into leaking sensitive data through an outbound channel.

## Four Properties Per Tool

<!-- Source of truth: ServiceTrustConfig in src/pynchy/workspace/types.py — keep these properties/defaults in sync. -->
Each configured tool declares four trust properties in
`data/personalization/pynchy.toml`:

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

## Approving a Request

Pynchy posts an approval prompt in the workspace that requested the action. For
a host action with a semantic capability ID, Discord and Slack offer four
choices:

- **Approve once** runs only the reviewed request.
- **Approve this session** also allows that exact capability for the active
  agent session.
- **Approve forever** adds that exact capability to the owning workspace's
  personalized `permissions.allow` list, validates the personalization tree,
  and publishes the Git change before reporting success.
- **Deny** rejects the request.

A permanent grant cannot weaken a stricter inherited `ask` or `deny` rule. In
that case, Pynchy rejects the request and reports why it could not save
the grant. A validation, commit, or push failure restores the prior local
policy and reports that the permanent approval failed. Approval prompts
without a stable semantic capability, including package artifact and Bash
escalations, remain one-shot **Approve** or **Deny** decisions.

The control records the decision in the originating workspace, so a prompt
from one chat cannot approve an action in another.

An exact-request approval covers only the reviewed payload. Pynchy rejects a
changed payload, a copied decision from another workspace, or a replayed
approval. The same guarded-action ID appears across the related security audit
events so an operator can trace the decision through execution.

For package artifact prompts, the reviewed payload is the normalized package
coordinate list attached to the blocked in-flight hook, not the full shell
string. Approval resumes only that waiting tool call and is not a reusable
shell approval.

Text-only channels show the command fallback in the prompt. Capability-backed
prompts use `approve-once a1`, `approve-session a1`, `approve-forever a1`, or
`deny a1`. One-shot prompts retain `approve a1` and `deny a1`. Prompts expire
after five minutes if no decision arrives.

## Permissions

Selecting a tool makes it available to a workspace. Calls default to `ask`
unless an explicit permission matches. Put reusable tool selection in a
profile, then put authorization on the profile or exact workspace that should
receive it.

```toml
[profiles.finance-assistant]
tools = ["email"]
permissions = { ask = ["mcp.email.send"], deny = ["mcp.email.delete"] }

[workspaces.automated-reports]
profiles = ["finance-assistant"]
permissions = { allow = ["mcp.email.preview"] }
```

Capability IDs use dotted segments. Host actions publish their IDs through the
capability status surface; MCP tool calls use
`mcp.<tool-name>.<call-name>`. A trailing `.*` applies to all matching calls,
such as `mcp.email.*`.

Pynchy accepts `allow`, `ask`, and `deny` arrays. Duplicate values within an
array or across arrays in one permission object fail validation. Pynchy
intersects every exact and wildcard rule that matches a capability. The most
restrictive explicit decision wins (`deny`, then `ask`, then `allow`),
independent of profile order.

Each decision has authoritative semantics:

- `allow` permits the matching capability without human approval, including
  approval that service trust or outbound payload scanning would otherwise
  request. It does not override a service property set to `"forbidden"` or
  disable Cop review.
- `deny` blocks the matching capability.
- `ask` requires approval. The operator chooses whether a capability-backed
  approval covers one exact request, the active session, or future sessions.

An unspecified permission defaults to `ask`. A runtime policy can replace that
implicit default with `allow`, but configuration fails if it tries to weaken an
explicit `ask` or `deny`. Service properties set to `"forbidden"`, Cop review,
and `always` approval contracts remain authoritative.

Denied MCP calls do not appear in `tools/list`, and direct calls remain blocked.
Denying `mcp.<tool-name>.*` removes the whole MCP server from that workspace.
Put narrower domains, paths, apps, or similar scopes in the tool's typed options;
generic permissions decide only `allow`, `ask`, or `deny`.

## Configuration Examples

### Personal calendar (fully trusted)

Your own Nextcloud calendar — you own the data, events aren't secrets, writes are safe.

```toml
[tools.caldav]
type = "caldav"
public_source = false
secret_data = false
public_sink = false
dangerous_writes = false

[tools.caldav.servers.nextcloud]
url = "https://nextcloud.example.com/remote.php/dav"
username = "me@example.com"
password_env = "CALDAV_PASSWORD"  # pragma: allowlist secret
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

Discord channels can instead receive automatic secret classification through
named Vaultwarden collections. See [Channel-scoped secrets](secrets.md).

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

## Agent Tool Gating

Agents can read and edit files, fetch URLs, install packages, and run shell commands. The agent tool gate normalizes those different operations before they run, using the same taint tracking as the tool trust policy above.

**File access establishes workspace taint.** File and shell operations notify the host before execution. If the selected profile sets `contains_secrets = true`, even a local command such as `ls` sets secret taint before a later external action.

A credential-looking shell argument or keyword match such as `.env`, `.env.production`, or `credentials` is initially only a `CRED001` taint candidate. The Cop sees the matched normalized command and confirms taint only when the operation can expose secret contents. It rejects incidental prose, search patterns, write-only destinations, and metadata-only operations. If that focused review is unavailable, ambiguous, invalid, or disabled, Pynchy confirms taint conservatively. A structured `Read` call targeting a recognized credential path is already conclusive and establishes taint without an LLM veto. The current operation still runs; the verdict only controls sticky state for later policy decisions. If no active host gate can retain that state, the operation is denied.

**Security gate failures deny the operation.** Bash requests are denied when the active host gate is missing or the host response is unavailable, empty, malformed, or unknown. CLI-backed cores also emit a denial for malformed hook input or an unexpected built-in gate exception. A malformed optional plugin-hook configuration falls back to the built-in security roster.

**Deterministic hazards never run.** The local gate blocks destructive system commands, reverse shells, remote content piped directly into a shell, and structured or shell-based writes to common persistence and autostart paths. This includes redirects, appends, `tee`, `cp`, and `install` destinations.

**Safe commands still run without command approval.** Common dev tools — `ls`, `cat`, `grep`, `sed`, `jq`, `find`, `git`, `wc`, and dozens more — are on a local whitelist. They cannot reach the network. Their file-access notification must still reach the active host gate. A `CRED001` candidate may receive the narrow taint review described above, but that review classifies secret exposure rather than approving the command.

**Trusted unattended profiles can disable Cop.** Set `cop_active = false` on a
profile only when the agent must operate without interactive Cop decisions and
the host account forms the intended outer authority boundary. Deterministic
command, persistence, and package checks still run before shell commands reach
the host. Explicit human-only MCP and host-action contracts remain unchanged.
Cop defaults to active. Possible credential access confirms secret taint
conservatively when this setting disables the reviewer.

**Package installs carry typed provenance.** Pynchy recognizes `uv`, `uvx`,
pip, pipx, npm, Yarn, and Cargo operations plus writes to their common manifests
and lockfiles. Shell-generated or ambiguous package names are denied. Direct
URLs, VCS, local or custom-registry sources, unpinned executable installs, and
releases less than seven days old require approval. Custom index flags,
registry environment variables, requirements directives, and uv index/source
tables count as custom registries. Registry outages also require approval for
executable installs; lock-pinned manifest reconciliation can continue with a
degraded audit event. Only the normalized registry coordinate is queried.
Pynchy does not use a third-party package reputation service, and package
checks do not change skill admission.

**Network commands are gated when tainted.** Commands like `curl`, `wget`, `python`, `ssh`, `pip install`, and similar network-capable tools are checked against the session's taint state:

- **No taint** — the command runs. Nothing sensitive to exfiltrate.
- **Any taint** — the Cop compares the exact command with current user intent, recent agent updates, and trusted host security facts. It treats routine local inspection, implementation, and validation as part of the authorized workflow even when those steps do not directly produce the requested final result. It denies clear unsafe conflicts and sends consequential ambiguity to you. Secret taint means sensitive data is accessible, not that every command reads or exposes it.

**Unknown commands get the same Cop triage.** Commands not on either list go to
the Cop with their classification and taint facts. A Cop approval covers only
that exact command and does not grant future commands. If the Cop fails, returns
an invalid verdict, or cannot load bounded intent context, Pynchy asks you
instead.

The agent tool gate stays active without configuration. To route Cop inspections
through a separate low-latency model, configure a model name exposed by your
LiteLLM gateway:

```toml
[security]
cop_model = "gpt-5.3-codex-spark"
cop_wire_api = "responses"
```

Without `cop_model`, the Cop uses `[agent].model`, then its built-in fallback
when the agent model is unset. Set `cop_wire_api` to `responses` for LiteLLM
routes declared with `mode: responses`; its default, `messages`, supports
Anthropic Messages routes. For technical details, see [Agent Tool Security Gate](../architecture/security.md#5b-agent-tool-security-gate).

## Host-Mutating Operations

Some IPC operations publish code or change persistent host behavior: opening
pull requests, registering new workspaces, scheduling tasks, and running host
commands. These are automatically inspected by the **Cop** — an LLM-based
security reviewer.

The Cop examines the payload of each host-mutating operation (the diff being merged, the task prompt, the group config) together with a small recent context window. Flagged operations require human approval before proceeding. If Cop or its context is unavailable, request-reply operations also require approval; fire-and-forget operations are blocked.

**What's covered:**
- Pull-request publication (`sync_worktree_to_main`)
- Workspace registration (`register_group`)
- Periodic agent creation (`create_periodic_agent`)
- Host-job scheduling (`schedule_host_job`)
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
