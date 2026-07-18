# Pynchy Security Model

Pynchy's security boundaries, trust model, and credential handling. Read this to understand what agents can and cannot access, and how to evaluate the risk of adding mounts, plugins, or new groups.

## Trust Model

| Entity | Trust Level | Rationale |
|--------|-------------|-----------|
| Admin group | Trusted | Private self-chat, admin control |
| Non-admin groups | Untrusted | Other users may be malicious |
| Container agents | Sandboxed | Isolated execution environment |
| WhatsApp messages | User input | Potential prompt injection |

## Security Boundaries

### 1. Container Isolation (Primary Boundary)

Agents execute in Apple Container (macOS) or Docker (Linux), providing:

- **Process isolation** — container processes cannot affect the host
- **Filesystem isolation** — only explicitly mounted directories appear inside the container
- **Full container privileges** — runs as root inside the container; container isolation is the security boundary
- **Ephemeral containers** — fresh environment per invocation (`--rm`)

The container boundary limits the attack surface to what's mounted, rather than relying on application-level permission checks.

### Direct Host Execution

Admin workspaces can set `execution_mode = "host"` with an explicit `cwd`. This mode runs the selected agent core as a host child process and does not use container isolation, file IPC, mounts, or Pynchy's built-in MCP server. Use it only for trusted operator workspaces where direct host access is the point of the workspace.

### 2. Mount Security

**External Allowlist** — Mount permissions live at `~/.config/pynchy/mount-allowlist.toml`:

- Stored outside the project root
- Never mounted into containers
- Agents cannot modify it

<!-- Source of truth: SecurityConfig.blocked_patterns in src/pynchy/config/models.py — keep this list in sync. -->
**Default Blocked Patterns:**
```
.ssh, .gnupg, .gpg, .aws, .azure, .gcloud, .kube, .docker,
credentials, .env, .netrc, .npmrc, .pypirc, id_rsa, id_ed25519,
private_key, .secret
```

**Protections:**

- Symlink resolution before validation (prevents traversal attacks)
- Container path validation (rejects `..` and absolute paths)
- `non_admin_read_only` option enforces read-only for non-admin groups

### 3. Session Isolation

Each group has isolated per-core session homes at
`data/sessions/{group}/.claude/` and `data/sessions/{group}/.codex/`:

- Groups cannot see other groups' conversation history
- Session data includes full message history and file contents read
- This prevents cross-group information disclosure

### 4. IPC Authorization

The host verifies messages and task operations against group identity:

| Operation | Admin Group | Non-Admin Group |
|-----------|------------|----------------|
| Send message to own chat | ✓ | ✓ |
| Send message to other chats | ✓ | ✗ |
| Schedule task for self | ✓ | ✓ |
| Schedule task for others | ✓ | ✗ |
| View all tasks | ✓ | Own only |
| Manage other groups | ✓ | ✗ |

### HTTP Control Plane

The HTTP surface carries chat history, real-time events, capability and canary
evidence, operational status, and deployment actions. Pynchy treats the application
listener as a security boundary instead of delegating authorization to Tailscale or a
host firewall.

The server binds TCP to loopback and creates a mode-`0600` Unix socket by default. A
non-loopback bind requires `allow_public_bind`; remote deployment requires the
separate `allow_remote_deploy` capability. Either remote posture requires a strong
bearer token from an environment variable or permission-restricted file. Middleware
authenticates every non-readiness TCP route, rate-limits requests by transport peer,
compares credentials in constant time, and records policy decisions in the security
audit sink. Unix-socket requests rely on filesystem permissions.

`/health` deliberately exposes only a static readiness state. Detailed `/status`,
`/capabilities`, `/canaries/*`, and `/api/*` responses remain behind the control-plane
policy. See [Control Plane Access](../usage/control-plane.md) for operator setup.

### 5. Service Trust Policy (Lethal Trifecta Defenses)

Host-side service tools (calendar, Slack, browser, etc.) are gated by `SecurityPolicy`, which prevents the *lethal trifecta*: an agent with simultaneous access to **untrusted input**, **sensitive data**, and **untrusted output channels**.

Each service declares four trust properties in `config.toml`:

<!-- Source of truth: ServiceTrustConfig in src/pynchy/types.py — keep these properties/defaults in sync. -->
| Property | Question it answers |
|----------|-------------------|
| `public_source` | Can this service deliver content from untrusted parties? |
| `secret_data` | Would leaking this data cause harm? |
| `public_sink` | Can this service send data to untrusted parties? |
| `dangerous_writes` | Are writes irreversible or high-impact? |

Values are `false` (safe), `true` (risky — triggers gating), or `"forbidden"` (blocked entirely). Unknown services default to all-true (maximum gating).

**Taint tracking.** The policy tracks two independent flags per container invocation:

- **`corruption_tainted`** — set when the agent reads from a `public_source`. The container has seen attacker-controlled content.
- **`secret_tainted`** — set when the agent reads `secret_data` or accesses a workspace whose profile has `contains_secrets = true`.

**Gating matrix.** When the agent writes to a service, the policy evaluates:

| Condition | Gate |
|-----------|------|
| `dangerous_writes = "forbidden"` | **Blocked** — operation denied |
| `dangerous_writes = true` | **Human approval required** |
| `corruption_tainted` AND `secret_tainted` AND `public_sink` | **Human approval required** (trifecta) |
| `corruption_tainted` AND `public_sink` | **Cop review** (LLM-based content scan) |
| None of the above | **Allowed** |

A payload secrets scanner (`detect-secrets`) also runs on outbound writes. If it detects credential patterns (API keys, tokens), the write escalates to human approval regardless of taint state.

**Typed host-action boundary.** Service plugins register immutable
`HostActionDescriptor` values. Startup rejects duplicate capability or tool
IDs, missing semantic `ActionSpec` links, and write descriptors without the
existing IPC idempotency and terminal-audit contracts. Legacy handler
dictionaries are accepted only when an effective `ActionSpec` already exposes
the tool; unknown tools fail startup.

Descriptors do not define a second permission system. The capability snapshot
shown by `/capabilities` and `/status` is read-only diagnostic state.
`SecurityPolicy` evaluates current semantic capability rules and service trust
again at each dispatch. A profile capability `allow` suppresses human approval
from service trust but cannot override a `"forbidden"` service property. A
missing capability rule remains neutral. Approved replay also rechecks denial
and descriptor availability.

Each host-action plugin declares an approval scope in its `ApprovalContract`
for requests whose resolved policy still requires a person. The default
`exact_request` scope approves only the pending request, which suits one-shot
effects such as sending one email. An opted-in `session_tool` scope grants that
tool on the active container invocation's `SecurityGate`, which suits
multi-step tools such as computer use. A session grant disappears when the
container invocation ends, does not cover another tool, and cannot override a
policy denial added before or after approval.

Policy, approval, and terminal execution events use the existing security
audit sink. Descriptor-backed events include `capability_id`, `action_ids`,
the IPC request ID, decision, safe reason, and taint state. Handler exceptions
and `{"error": ...}` responses both record terminal failure without copying
provider error bodies into capability status.

Admin workspaces use the same tool trust declarations at runtime. They are additionally protected by the clean room policy ([§5d](#5d-admin-clean-room)), which prevents admin workspaces from selecting public-source tools. See [Tool Trust](../usage/security.md) for configuration.

### 5b. Bash Security Gate

The service trust policy (above) gates MCP service tools, but agents also have a general-purpose Bash tool. Without extra controls, a corruption-tainted agent could run `curl`, `python`, or `ssh` to exfiltrate data — bypassing the service trust layer entirely.

The bash security gate closes this gap. It runs as a `BEFORE_TOOL_USE` hook inside the container, intercepting every Bash tool call before execution. The Claude and OpenAI SDK cores and both CLI cores compose the same hook roster, so the gate applies regardless of the selected built-in core.

**Classification cascade.** The container classifies each command locally using a three-tier system:

1. **Regex whitelist** — provably local commands (`ls`, `cat`, `grep`, `sed`, `jq`, etc.) that cannot reach the network. These run immediately without IPC.
2. **Regex blacklist** — known network-capable commands (`curl`, `python`, `ssh`, `wget`, `pip install`, etc.). These always escalate to the host.
3. **Unknown** — commands on neither list. Also escalated to the host.

Pipelines and chains are split into segments; one network-capable segment makes the whole command network-classified.

**Host-side evaluation.** When a command escalates, the container sends a `security:bash_check` IPC request (see [IPC docs](ipc.md#security-requests)). The host evaluates the command against the session's taint state:

| Taint State | Network Command | Unknown Command |
|---|---|---|
| No taint | Allow | Allow |
| Corruption only | Cop review | Cop review |
| Corruption + secret | Human approval required | Cop review (human if Cop flags) |

The Cop is the same LLM-based inspector used for host-mutating operations. If the Cop flags a command in a dual-tainted session, the decision escalates to human approval. The 300-second approval timeout matches the existing service approval flow.

**Fail-open design.** If IPC fails (timeout, malformed response), the gate allows the command. Otherwise the security gate would break normal agent operation during transient failures.

### 5c. Host-Mutating Operations (Cop Gate)

Some IPC operations change what code runs on the host machine. These are **host-mutating** and get an extra layer of inspection from the Cop — an LLM-based security inspector that reviews payloads for signs of manipulation.

**Host-mutating operations:**

| Operation | What it mutates | Cop inspects |
|---|---|---|
| `sync_worktree_to_main` | Merges code into main branch | The merge summary |
| `register_group` | Creates new workspace | Group config |
| `create_periodic_agent` | Creates persistent agent | Agent name, profile, schedule, prompt |
| `schedule_task` | Schedules future execution | Task prompt and target |
| `schedule_host_job` | Schedules host command | Command and schedule |
| Script-type MCP tools | Runs host subprocess | Tool arguments |

**Auto-classified:** MCP servers with `type = "script"` run as host subprocesses. Any tool call to a script-type MCP is host-mutating by definition and goes through the Cop.

**Not host-mutating:** Docker-type MCPs (isolated container), URL-type MCPs (remote), and `deploy` (restarts with existing code on main).

**Escalation rule:**

| Cop verdict | Action |
|---|---|
| Not flagged | Operation proceeds |
| Flagged (request-reply) | Human approval required |
| Flagged (fire-and-forget) | Operation blocked, warning broadcast |

The Cop always inspects. Humans only get pulled in when the Cop detects something suspicious.

### 5d. Admin Clean Room

Admin workspaces cannot select tools with `public_source=true`. This is enforced at config validation (startup). Tools default to `public_source=true`, so admin profiles must select only tools explicitly marked `public_source = false`.

This prevents the most privileged workspace from ever being corruption-tainted, eliminating prompt injection as a threat vector for admin operations.

For tasks that need untrusted input (web browsing, email), create a non-admin workspace with appropriate trust declarations.

### 6. Credential Handling

#### LLM Gateway (default)

When `gateway.enabled = true` (the default), an LLM API gateway runs on the host and proxies container API calls to real providers. Containers **never see real LLM API keys**.

**How it works:**

```
Container ──[gateway key]──► Host Gateway ──[real API key]──► Provider
```

1. The gateway discovers real credentials (Anthropic API key / OAuth token, OpenAI API key) from `config.toml [secrets]` and auto-discovery (Claude Code keychain, etc.).
2. On startup, a random per-session ephemeral key (`gw-…`) is generated.
3. Containers receive environment variables pointing to the gateway:

```
ANTHROPIC_BASE_URL=http://<container-reachable-host>:4010
ANTHROPIC_AUTH_TOKEN=gw-<random>
OPENAI_BASE_URL=http://<container-reachable-host>:4010
OPENAI_API_KEY=gw-<random>
```

4. The gateway validates the ephemeral key, then forwards requests to the real provider with real credentials injected. Responses stream back transparently.
5. Required headers (`anthropic-beta`, `anthropic-version`) are forwarded to the provider.

**Security properties:**

- Real API keys exist only in host process memory
- Ephemeral keys regenerate on each restart and carry no value outside the gateway
- A compromised container cannot use the ephemeral key to reach providers directly
- Docker containers reach the host via `host.docker.internal` (with `--add-host` on Linux)
- Apple runtime containers use the host gateway address resolved by Pynchy when `container_host` keeps the default value

#### OneCLI Agent Vault

When `[onecli].enabled = true`, Pynchy uses OneCLI as the preferred
credential and egress boundary for non-LLM service credentials. Pynchy remains
the source of truth for semantic capability permissions. Pynchy does not read
decrypted secret values from OneCLI. Instead, before spawning a container it
asks OneCLI for:

- proxy environment variables such as `HTTPS_PROXY`;
- a gateway CA certificate and the container path where that certificate should
  be trusted;
- credential stub files whose contents are placeholders such as
  `onecli-managed`.

Containers and opt-in MCP sidecars then call normal service URLs. OneCLI matches
the outbound request and injects the real credential at request time. OneCLI
rules may provide low-level egress hardening, but Pynchy capability and service
trust policy make the user-facing permission decision. Pynchy maps each
workspace to a stable OneCLI agent identifier so OneCLI can keep credential
access separate per workspace.

With OneCLI material present, Pynchy does not write `GH_TOKEN` into the agent env
file. Native credential injection remains available only when OneCLI is disabled
or explicitly configured with `fail_closed = false` for migration.

For GitHub, the preferred OneCLI connection is a GitHub App installation rather
than user OAuth. OAuth is useful for quick personal setup, but a GitHub App gives
repo-scoped, short-lived installation tokens and maps more directly to Pynchy's
per-workspace repo boundaries.

Without OneCLI, **non-LLM credentials** get written directly to per-group env files (`data/env/{group}/env`):

| Credential | Admin | Non-Admin | Rationale |
|-----------|-----|---------|-----------|
| `GH_TOKEN` | Yes (broad) | **Repo-scoped** (if configured) | Admin gets the host's broad token. Non-admin containers with profile `repo` get a fine-grained PAT scoped to their designated repo (configured via `repos.overrides."owner/repo".token`). Non-admin containers without a repo get no token. |
| `GIT_AUTHOR_NAME` | Yes | Yes | Needed for git commits in worktrees |
| `GIT_COMMITTER_NAME` | Yes | Yes | |
| `GIT_AUTHOR_EMAIL` | Yes | Yes | |
| `GIT_COMMITTER_EMAIL` | Yes | Yes | |

Each group gets its own env directory, so concurrent containers don't share secrets. A compromised non-admin container's token is scoped to a single repo and cannot access other repositories.

**Token resolution order** for host-side git operations (fetch, push, ls-remote):

1. `repos."owner/repo".token` — explicit per-repo fine-grained PAT (highest priority)
2. `secrets.gh_token` — host's broad token (fallback for repos without a scoped token)
3. `gh auth token` — auto-discovered from `gh` CLI (lowest priority)

**NOT Mounted:**

- WhatsApp session (`data/neonize.db`) — host only
- Mount allowlist — external, never mounted
- Any credentials matching blocked patterns

### 7. Prompt Injection

Channel messages can contain malicious instructions that attempt to manipulate Claude's behavior.

**Mitigations:**

- Container isolation limits the blast radius of successful attacks
- Only registered groups get processed (explicit allowlist)
- Trigger word requirement reduces accidental processing
- Agents can only access their group's mounted directories
- Additional directory mounts require explicit per-group configuration
- Claude's built-in safety training helps resist manipulation
- **Admin clean room** prevents the admin workspace from reading untrusted content ([§5d](#5d-admin-clean-room))
- **Cop inspection** reviews host-mutating payloads for manipulation before execution ([§5c](#5c-host-mutating-operations-cop-gate))

**Recommendations:**

- Only register trusted groups
- Review additional directory mounts carefully before adding
- Review scheduled tasks periodically for unexpected behavior
- Monitor logs for unusual activity

## Privilege Comparison

| Capability | Admin Group | Non-Admin Group |
|------------|------------|----------------|
| Repo access | `/workspace/repos/<owner>/<repo>` (rw) | Via profile `repo` (worktree, rw) |
| Group folder | `/workspace/group` (rw) | `/workspace/group` (rw) |
| System prompts | Scoped via config | Scoped via config |
| `config.toml` | Mounted read-write | Not mounted |
| Additional mounts | Configurable | Read-only unless allowed |
| Network access | Unrestricted | Unrestricted |
| MCP service tools | Auto-approved | Trust-gated (see [§5](#5-service-trust-policy-lethal-trifecta-defenses)) |
| Public-source MCPs | Not allowed (clean room) | Trust-gated |

## Security Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        UNTRUSTED ZONE                             │
│  WhatsApp Messages (potentially malicious)                        │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 ▼ Trigger check, input escaping
┌──────────────────────────────────────────────────────────────────┐
│                     HOST PROCESS (TRUSTED)                        │
│  • Message routing                                                │
│  • IPC authorization                                              │
│  • Mount validation (external allowlist)                          │
│  • Container lifecycle                                            │
│  • LLM Gateway (credential-isolating reverse proxy)               │
│    ┌──────────────────────────────────────────────────┐           │
│    │ Container ──[gw key]──► Gateway ──[real key]──► Provider │   │
│    └──────────────────────────────────────────────────┘           │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
                                 ▼ Explicit mounts only, gateway URL in env
┌──────────────────────────────────────────────────────────────────┐
│                CONTAINER (ISOLATED/SANDBOXED)                     │
│  • Agent execution                                                │
│  • Bash commands (sandboxed)                                      │
│  • File operations (limited to mounts)                            │
│  • LLM API access via gateway only (no real keys)                 │
│  • Cannot modify security config                                  │
└──────────────────────────────────────────────────────────────────┘
```
