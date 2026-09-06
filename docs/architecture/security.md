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

`pynchy publish-personalization` is a host-operator command. Standard agent
Bash hooks deny its supported CLI invocation. This hook does not isolate trusted
direct-host or raw host-mount contexts, which can alter host files; grant those
contexts only to operators trusted with the personalization checkout.

The Bash hook provides command-policy feedback, not a sandbox for arbitrary
executables or interpreter code. Normal containers lack the canonical checkout
and host publication token; treat the host-execution boundary, not shell text
inspection, as the authorization boundary.

Treat the independent personalization checkout's Git metadata, including
`origin`, as host-operator metadata. Non-admin containers receive only its
`skills/` directory. Admin containers receive a trusted raw host-repository
mount, while direct-host workspaces access host files without mount isolation.
Both modes must be trusted not to alter that checkout.

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

The HTTP surface carries capability and canary evidence, operational status,
provider webhooks, and deployment actions. Pynchy treats the application listener
as a security boundary instead of delegating authorization to Tailscale or a host
firewall.

The server binds TCP to loopback and creates a mode-`0600` Unix socket by default. A
non-loopback bind requires `allow_public_bind`; remote deployment requires the
separate `allow_remote_deploy` capability. Either remote posture requires a strong
bearer token from an environment variable or permission-restricted file. Middleware
authenticates every non-readiness TCP route except exact plugin-registered webhook
POST paths, rate-limits requests by transport peer, compares credentials in constant
time, and records control-plane policy decisions in the security audit sink.
Unix-socket requests rely on filesystem permissions.

Webhook POST paths replace the bearer token with provider-owned authentication;
for example, Linear signs the raw body with a per-subscription secret. Startup
rejects missing secrets, duplicate paths, unknown workspaces, and fixed routes
to admin workspaces. A provider-derived route must declare every candidate
workspace and explicitly opt into admin candidates; the host checks the resolved
owner against that allowlist for every delivery. Admin candidates still must pass
the clean room's source-trust validation. The host then enforces bounded bodies,
a second per-route rate limit, durable delivery-ID deduplication, and isolated
task admission. Each route resolves its provider account's data-flow declaration:
public payloads are fenced, while content from a private account retains
authenticated provenance without corruption taint. Neither form can bypass
explicit execution gates. See
[Provider-authenticated webhooks](../usage/control-plane.md#provider-authenticated-webhooks).

`/health` deliberately exposes only a static readiness state. Detailed `/status`,
`/capabilities`, `/actions`, `/work-items`, and `/canaries/*` responses remain
behind the control-plane policy. See [Control Plane Access](../usage/control-plane.md)
for operator setup.

### 5. Service Trust Policy (Lethal Trifecta Defenses)

Host-side service tools (calendar, Slack, browser, etc.) are gated by `SecurityGate`, which prevents the *lethal trifecta*: an agent with simultaneous access to **untrusted input**, **sensitive data**, and **untrusted output channels**.

Each service declares four trust properties in
`data/personalization/pynchy.toml`:

<!-- Source of truth: ServiceTrustConfig in src/pynchy/workspace/types.py — keep these properties/defaults in sync. -->
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
IDs, missing semantic `ActionSpec` links, raw handler mappings, and write
descriptors without the existing IPC idempotency and terminal-audit contracts.

Descriptors do not define a second permission system. The capability snapshot
shown by `/capabilities` and `/status` is read-only diagnostic state.
`SecurityGate` evaluates current semantic permissions and service trust again
at each dispatch. Selected tools default to `ask`. Explicit profile, workspace,
and runtime rules intersect with `deny` more restrictive than `ask`, and `ask`
more restrictive than `allow`. An explicit `allow` suppresses human approval
from service trust but cannot override a `"forbidden"` service property. Runtime
policy may authorize an implicit default but cannot weaken an explicit rule.
Approved replay also rechecks denial and descriptor availability.

Each host-action plugin declares an approval trigger and scope in its
`ApprovalContract`. The default `service_policy` trigger combines capability
rules with service trust. `capability_only` is reserved for bounded,
workspace-local state: it suppresses automatic human gates from service trust
and payload scanning, while explicit permission `ask` and `deny` rules,
service prohibitions, and Cop review remain authoritative. `always` requires a
person even when the other policies would allow the request.

The default `exact_request` scope approves only the pending request, which
suits one-shot effects such as sending one email. An opted-in `session_tool`
scope grants that tool on the active container invocation's `SecurityGate`,
which suits multi-step tools such as computer use. A session grant disappears
when the container invocation ends, does not cover another tool, and cannot
override a policy denial added before or after approval. A descriptor can also
declare fallback service trust for a built-in provider; workspace configuration
with the same service name takes precedence.

Policy, approval, and terminal execution events use the existing security
audit sink. Descriptor-backed events include `capability_id`, `action_ids`,
the guarded-action ID, decision, redacted reason, and taint state. One guarded
action ID spans the agent hook checks, host policy and Cop decisions, approval
record, execution response, and audit events. Handler exceptions and
`{"error": ...}` responses both record terminal failure without copying
provider error bodies into capability status.

Exact-request approvals store a canonical SHA-256 hash of the complete JSON
request. Approved host-mutating replays receive a process-local, single-use
receipt bound to that hash, guarded-action ID, operation, and workspace. The
destination consumes the receipt before execution; replay, mutation, and
cross-workspace use fail closed. Caller-supplied approval booleans are not
trusted.

**Authenticated external routes.** A connection runtime may authenticate an
external provider event before routing it into a conversation workspace.
Authentication proves provider origin; the route's source-trust declaration
decides whether the provider can carry attacker-controlled content. Admin targets
require a trusted source policy and explicit plugin opt-in.
After authentication, a provider may discard an actor outside that policy before
workspace resolution and durable receipt admission. Discarded deliveries leave no
provider event record or host effect.
Public-source routes fence provider context and start the invocation
corruption-tainted. Trusted routes retain provider provenance without adding
public-source taint. Matrix route input always starts both corruption-tainted and
secret-source-tainted. External input bypasses only ordinary channel sender
allowlists. Host commands such as approval, denial, identity, and deploy commands
are not parsed from an external message body, even on a trusted route.

An operator can still approve or deny a pending action in the same Discord
control thread while the routed agent turn is active. The host executes that
control message but excludes it from agent input. Lifecycle controls that would
replace context run after the active turn commits. Matrix writes use the
`always` trigger with exact-request scope even when other service policy would
allow the call. The approved replay must still match the conversation control binding,
unexpired action payload, current effective workspace policy, and live route
portal.

Admin workspaces use the same tool trust declarations at runtime. They are additionally protected by the clean room policy ([§5d](#5d-admin-clean-room)), which prevents admin workspaces from selecting public-source tools. See [Tool Trust](../usage/security.md) for configuration.

### 5b. Agent Tool Security Gate

The service trust policy (above) gates MCP service tools, but agents also have file, shell, editing, URL, and package tools. Without a shared boundary, a corruption-tainted agent could read workspace secrets and later run `curl`, `python`, or `ssh` to exfiltrate them.

The agent tool security gate closes this gap. It runs as a `BEFORE_TOOL_USE` hook in every agent runner, whether that runner is inside a container or is a trusted direct-host child process. The Claude and OpenAI SDK cores and both CLI cores compose the same hook roster, so the gate applies regardless of the selected built-in core or execution mode.

**Semantic artifact normalization.** The gate parses core-specific tool names and input shapes into owned artifact types: commands, read and write paths, written content, URLs, and package references. Policy therefore follows the operation when an SDK renames a shell or patch tool. Free-form patch payloads remain written content even when a CLI hook transports them through a field named `command`; prose and file contents never become shell commands merely because of that transport shape. Stable deterministic rules then block destructive system commands (`CMD001`), reverse shells (`NET001`), remote content piped directly into a shell (`NET002`), writes to persistence or autostart paths (`PERSIST001`), and mutations of generated Codex skill registries (`SKILL001`). The latter must use `$PYNCHY_SKILLS_ROOT` for durable authoring. Persistence detection covers structured writes and shell redirection, append, `tee`, `cp`, and `install` destinations. Credential-looking command arguments and read paths emit `CRED001`, including `.env.*` variants. A structured read path is conclusive evidence; a command-token match is a heuristic candidate. Audits retain the rule ID and adjudication verdict without copying matched file content.

**File taint notification.** Every normalized file operation and every shell operation notifies the host before execution. The host calls `SecurityGate.notify_file_access()`, so a workspace with `contains_secrets = true` becomes secret-tainted even when the later Bash classifier considers a command such as `cat .env` or `ls` provably local. That profile declaration is a host-owned fact.

Command-token `CRED001` matches are different: they are heuristic evidence, not taint facts. Before one can set sticky secret taint, a dedicated Cop prompt receives the tool name and bounded normalized command and decides whether the proposed operation can actually expose secret contents. Reading or loading `.env` confirms taint. Merely searching for the word `credentials`, mentioning `.env`, writing documentation, or using a matched path only as an output destination rejects the candidate. The prompt classifies data flow only; user intent and dangerousness are irrelevant. Detected sensitive literal values are locally redacted from the evidence sent to the Cop. Invalid evidence, Cop failure, invalid output, genuine ambiguity, or a profile with Cop disabled confirms taint conservatively. A rejection is audited but does not mutate session state. Structured `Read` calls targeting recognized credential paths are already unambiguous and establish taint without an LLM veto.

Confirmed taint remains sticky for the container invocation. The host rejects the operation when no active invocation gate can retain taint state; it never substitutes a throwaway policy object. Bash policy requests follow the same rule: a missing gate, unavailable host, empty or malformed response, or unknown decision denies the command because an approval cannot be obtained safely.

**Unattended Cop mode.** Profiles use Cop by default. A profile can set
`cop_active = false` when Cop decisions would prevent an intentionally
unattended automation from completing. This disables secondary Cop inspection
for that workspace but does not disable deterministic command, persistence, or
package rules, or explicit human-only MCP and host-action contracts. Heuristic
secret-taint candidates confirm conservatively when Cop is disabled; disabling
Cop never turns a possible secret exposure into an untainted session.

**CLI hook failure behavior.** CLI-backed cores emit an explicit denial when the hook input is malformed or a built-in security hook raises unexpectedly. Plugin hook modules are declared through typed host-side specifications. Missing or unloadable optional modules are skipped, while the built-in roster remains active, so an optional extension cannot disable the owned gate.

**Package provenance and release age.** Package commands normalize `uv add`,
`uv tool install`, `uvx`, pip, pipx, npm, Yarn, and Cargo into an ecosystem,
normalized name, exact version when present, source class, intent, and lock-pin
state. Writes and patches to `pyproject.toml`, `uv.lock`, requirements files,
`package.json`, npm and Yarn locks, `Cargo.toml`, and `Cargo.lock` produce the
same owned references. Deterministic rules reject shell-evaluated (`PKG002`)
and ambiguous or missing (`PKG003`) coordinates. Direct URL, VCS, local, and
custom-registry sources (`PKG001`) and unpinned executable installs (`PKG004`)
require human approval. Custom registry detection covers package-manager index
and registry flags, pip/uv/npm registry environment prefixes, requirements
index directives, and uv index/source tables in `pyproject.toml`; those
packages are never mislabeled as authoritative PyPI, npm, or crates.io results.

For an exact registry coordinate, the host queries only the authoritative
PyPI, npm, or crates.io endpoint. Requests have a three-second timeout, a
256-KiB response limit, strict response parsing, and a six-hour
content-addressed cache keyed only by ecosystem, normalized name, and version.
No command, prompt, workspace content, or secret crosses this boundary. A
release less than seven days old (`PKG006`) requires approval. When registry
metadata is unavailable (`PKG005`), executable and direct installs require
approval; an already lock-pinned manifest reconciliation may continue and
records an explicit degraded audit event. The cache is an availability
optimization, not provenance proof. Pynchy does not consult a third-party
package reputation service.

Package checks do not admit or vet personalized or agent-authored skills. Skill discovery and skill
access remain their existing separate capability surface. Documentation and
local caches may make repeated inspection cheaper, but neither counts as
security evidence.

A package artifact approval binds the normalized package coordinates and the
guarded in-flight hook request. It does not claim that the human reviewed an
unseen full shell string. The blocked hook can resume only that current tool
call; approval cannot be replayed as a general shell authorization.

**Classification cascade.** The container classifies each command locally using a three-tier system:

1. **Regex whitelist** — provably local commands (`ls`, `cat`, `grep`, `sed`, `jq`, etc.) that cannot reach the network. After the file-taint notification, these run without Cop review.
2. **Regex blacklist** — known network-capable commands (`curl`, `python`, `ssh`, `wget`, `pip install`, etc.). These always escalate to the host.
3. **Unknown** — commands on neither list. Also escalated to the host.

Pipelines and chains are split into segments; one network-capable segment makes the whole command network-classified.

**Host-side evaluation.** When a command escalates, the container sends a `security:bash_check` IPC request (see [IPC docs](ipc.md#security-requests)). The host evaluates the command against the session's taint state:

| Taint State | Network Command | Unknown Command |
|---|---|---|
| No taint | Allow | Allow |
| Secret only | Cop triage | Cop triage |
| Corruption only | Cop triage | Cop triage |
| Corruption + secret | Cop triage | Cop triage |

The command Cop returns one of three verdicts for the exact command. It reasons
about intent at the workflow level: ordinary local inspection, implementation,
testing, linting, formatting, and verification can support an authorized result
without directly producing that result. It approves harmless supporting work,
denies commands that clearly conflict with the workflow or create an
unacceptable security risk, and escalates consequential ambiguity to human
approval. The bounded packet includes recent agent updates so in-progress work
does not look unrelated merely because the latest user message names a final
result such as an issue or deployment.

Corruption and confirmed secret taint remain trusted host facts in the review packet.
Secret taint means the session can access sensitive data; it does not claim that
the proposed command reads or exposes that data. Taint raises scrutiny when a
command can create a dangerous data flow. A network-capable command in a
dual-tainted session receives the most cautious review. Cop approval never
creates a reusable grant. The 300-second approval timeout matches the existing
service approval flow.

**Degraded behavior.** Deterministic blocking rules run locally. If the host
cannot record and retain an artifact notification, the artifact hook fails
closed. A degraded heuristic-taint review confirms taint; it does not deny the
current operation. Invalid Cop output, Cop failure, or bounded-context loss
during command review requires human approval. Fire-and-forget host mutations
remain blocked.

### 5c. Host-Mutating Operations (Cop Gate)

Some IPC operations change what code runs on the host machine. These are **host-mutating** and get an extra layer of inspection from the Cop — an LLM-based security inspector that reviews payloads for signs of manipulation.

**Host-mutating operations:**

| Operation | What it mutates | Cop inspects |
|---|---|---|
| `sync_worktree_to_main` | Pushes a worktree branch and opens or updates a PR | The committed `base...HEAD` patch |
| `publish_managed_feature` | Pushes one managed feature branch and opens or updates a PR | Host-derived manifest identity and committed `base...HEAD` patch |
| `register_group` | Creates new workspace | Group config |
| Automation mutations | Creates, updates, pauses, resumes, or deletes config-backed automations | Automation name and definition |
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
| Cop or bounded context unavailable (request-reply) | Human approval required |
| Cop or bounded context unavailable (fire-and-forget) | Operation blocked, warning broadcast |

When Cop blocks a request-reply publication, the lifecycle tool receives an
immediate no-publication result instead of waiting for its timeout. The
payload-bound approval remains pending and can replay the exact operation if a
human approves it later.

`sync_worktree_to_main` and `publish_managed_feature` are PR-only at the host
boundary. Missing or alternate publication modes are rejected before approval
receipts or Cop authority are evaluated, so neither action can merge into the
host branch or trigger a deployment.

`rebase_managed_feature` is a separate local-only host operation. It accepts
only a canonical feature slug and derives the worktree and remote default
branch from the active manifest. It requires a clean worktree and supplies the
verified remote base through host-owned Git metadata while disabling hooks,
replacement refs, and ambient configuration. It never pushes, opens a pull
request, merges, or deploys. A conflict remains in the manifest-bound
worktree, where the agent can use only the existing rebase recovery commands.

**Bound managed-feature publication.** The agent-facing
`publish_managed_feature` tool accepts only a canonical feature slug. The host
reads only `.new-feature/manifest.toml` beneath configured repository roots and
requires one active version-2 record whose normalized key and slug match. It
derives the repository, exact `.worktrees/<slug>` location, checked-out branch,
and target branch itself. The resolver queries the configured repository's
fixed GitHub URL for symbolic `HEAD`, requires the manifest target to match its
remote default branch, and fetches that branch's exact SHA into an isolated Git
directory. It never treats local `main` or `origin/HEAD` refs as authority. The
agent cannot choose a repository, path, branch, target, merge mode, or
deployment mode. Missing or ambiguous records fail closed, and the host never
scans or falls back through worktree directories.

The fetched remote target SHA must be an ancestor of the raw feature HEAD. A
feature that predates an advanced target must be rebased before the host
inspects or publishes it.

Before Cop inspection, the host adds its derived `feature_slug`, `repository`,
`branch`, `target_branch`, `base_sha`, and `head_sha` to the Tier 2 request. A
pending approval receipt binds those exact fields as part of the request,
preventing a receipt for one managed feature state from publishing another.
The resolver disables Git replacement refs, validates the raw commits and
repository object format, and checks worktree cleanliness through a fresh,
host-created Git directory and index. It never loads the managed worktree's
Git configuration while checking files, so agent-owned hooks, filesystem
monitors, clean filters, and replacement refs cannot run as the host. A
configured custom clean filter does not execute and does not by itself block
publication.

Before push, the publisher resolves the managed feature again and requires the
same recorded base and HEAD SHAs. A changed branch, base, or manifest state
blocks publication and requires a new inspection. It creates a fresh temporary
bare repository with the validated source object format, disables system and
global Git configuration, and exposes only the validated object store as an
alternate source. Before every Git operation that uses that alternate, it
revalidates the object-store directory and its `objects/info` directory, and
rejects symlinks or any `objects/info/alternates` entry. From that isolated
repository, it rechecks the target's remote SHA before pushing and opening a
PR, reads the exact remote feature branch ref, and pushes the inspected HEAD
through an exact
`--force-with-lease=refs/heads/<branch>:<remote-sha>`. This prevents mutable
worktree config, `url.*.pushInsteadOf` rewrites, hooks, and replacement refs
from redirecting or changing the publication. `gh` runs from the temporary
directory with `--repo` fixed to the configured repository. This path can only
open or update a pull request; it cannot merge, deploy, or select a fallback
worktree.

With Cop active, `sync_worktree_to_main` supplies each configured repository's
committed worktree patch, while `publish_managed_feature` supplies only its
selected manifest-bound patch. Both disable Git replacement refs, external diff
drivers, and text conversion. Managed-feature patch capture streams output and
stops at 64 KiB instead of buffering a full committed diff in host memory. A
missing or failed diff, binary content, or more than 64 KiB of combined patch
text requires human approval instead of asking Cop to judge incomplete
evidence. This inspection limit doesn't reject a change based on its size; it
changes who must approve publication. A valid single-use approval receipt can
replay the exact request without another inspection.

The Cop receives a bounded SQLite view of the current user intent, the four
most recent user or assistant messages (500 characters each), the eight most
recent completed tool names, and any active host-derived Linear execution
authority for that exact chat. The authority is present only while the linked
scheduled task, in-flight occurrence, and execution lease are all active. Its
scope includes publishing that work item's isolated branch as a pull request,
but excludes merging, deploying, and unrelated external writes. Tool inputs
and full history do not cross this boundary. The proposed action is included
separately. Missing context is explicit rather than replaced with guessed
values.

### 5d. Admin Clean Room

Admin workspaces cannot select tools with `public_source=true`. This is enforced at config validation (startup). Tools default to `public_source=true`, so admin profiles must select only tools explicitly marked `public_source = false`.

This prevents the most privileged workspace from ever being corruption-tainted, eliminating prompt injection as a threat vector for admin operations.

For tasks that need untrusted input (web browsing, email), create a non-admin workspace with appropriate trust declarations.

### 6. Credential Handling

#### LLM Gateway

An LLM API gateway runs on the host and proxies container API calls to real providers. Containers **never see real LLM API keys**.

**How it works:**

```
Container ──[gateway key]──► Host Gateway ──[real API key]──► Provider
```

1. LiteLLM resolves provider credentials from
   `data/personalization/litellm.yaml` and the root `.env`.
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

#### LLM request redaction

The built-in gateway owns the authenticated Python request boundary. It reads
each complete provider-native JSON request and replaces detected credentials,
private keys, email addresses, phone numbers, Social Security numbers, and
payment-card numbers before forwarding the request. Redaction covers system
and instruction fields, prompts, message content, Responses API input, and
tool results. Provider response chunks pass through unchanged, which preserves
streaming and tool-call framing.

Each request receives a random placeholder namespace and an isolated in-memory
map while the body is transformed. Spans contain only offsets and data classes,
not matched values. The production gateway takes only the redacted bytes and
immediately discards that map, so gateway redaction is irreversible. Pynchy
does not restore placeholders into model-generated tool arguments, public or
remote sinks, third-party prompts, audit logs, or errors; placeholders returned
by a model remain placeholders.

The redaction module retains a generic request-local restoration primitive for
isolated tests and a future trusted sink integration. Its caller-constructed
descriptor is not production authority, and no active gateway path retains the
session or accepts that descriptor. A future reversible flow must bind the
session to an owned non-public sink descriptor and authoritative capability
policy before it can make a production restoration claim.

LiteLLM runs as a separate Docker proxy and does not expose an owned Python
request callback in Pynchy's current integration. Redaction therefore reports
`not_enforced` in `/status` for LiteLLM mode. It reports `enforced` only for the
built-in gateway. Do not treat LiteLLM routing as protected by this redaction
layer unless the integration gains an enforceable request hook.

#### Non-LLM credentials

| Credential | Admin | Non-Admin | Rationale |
|-----------|-----|---------|-----------|
| `GIT_AUTHOR_NAME` | Yes | Yes | Needed for git commits in worktrees |
| `GIT_COMMITTER_NAME` | Yes | Yes | |
| `GIT_AUTHOR_EMAIL` | Yes | Yes | |
| `GIT_COMMITTER_EMAIL` | Yes | Yes | |

Tools own every provider credential that can enter an agent or tool process.
TOML names the requirements; a selected, available tool supplies its companion
skills and exact process exposure. Skill files and learned skills cannot grant
credentials. Missing requirements produce value-free notices.

Runtime-backed tool credentials stay in the runtime by default. Workspace tool
credentials enter the agent process because no separate service exists.
Pynchy filters host environments, keeps Docker values out of argv, and creates
no workspace credential files. Production deployments materialize values from
Proton Pass into the Pynchy host process before startup. Pynchy never resolves a
workspace-owned Pass template.

See [Tool access and secrets](../usage/tool-access.md) for the canonical
configuration and delivery rules.

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
| Repo access | `/home/agent/src/<owner>/<repo>` (rw) | Via profile `repo` (worktree, rw) |
| Group folder | `/home/agent/workspace` (rw) | `/home/agent/workspace` (rw) |
| System prompts | Scoped via config | Scoped via config |
| Personalization files through the project mount | Read-write | Not mounted |
| Canonical personalization skills | Read-write | Read-write |
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
