# Lethal Trifecta Defenses

## Problem

An agent becomes dangerous when it simultaneously has:
- **A) Untrusted input** — data from sources we don't control (emails from strangers, web content)
- **B) Sensitive data** — information that could cause harm if leaked (passwords, .env files)
- **C) Untrusted sinks** — channels that could be used for exfiltration or harm (sending email, posting publicly)

Any two without the third is manageable. All three together is the "lethal trifecta" — a prompt-injected agent that holds secrets and can write to external channels.

## Solution: Four Properties Per Service

Each service declares four properties. Users answer intuitive questions, not security jargon:

| Property | Question | Default |
|----------|----------|---------|
| `public_source` | Can untrusted parties provide input through this? | `true` (cautious) |
| `secret_data` | Does this hold sensitive/secret information? | `true` (cautious) |
| `public_sink` | Can data I send reach untrusted parties? | `true` (cautious) |
| `dangerous_writes` | Are writes high-stakes or irreversible? | `true` (cautious) |

### Tri-state values

All four properties accept three values:

| Value | Meaning |
|-------|---------|
| `false` | Not risky — no gating |
| `true` | Risky — gating applies |
| `"forbidden"` | Completely forbidden — blocked unconditionally |

### Configuration examples

```toml
# Global service declarations
[services.calendar]
public_source = false       # only I add events
secret_data = false          # events aren't secrets
public_sink = false          # writes only affect my calendar
dangerous_writes = false     # creating events is low-stakes

[services.email]
public_source = true         # anyone can email me
secret_data = true           # inbox has private correspondence
public_sink = true           # can send to anyone
dangerous_writes = true      # sending email is high-stakes

[services.passwords]
public_source = false        # vault data is trusted
secret_data = true           # passwords ARE secrets
public_sink = false          # can't send passwords via this service
dangerous_writes = false     # read-only service

[services.web_search]
public_source = true         # web content is untrusted
secret_data = false          # doesn't hold secrets
public_sink = false          # can't post data via search
dangerous_writes = false     # N/A

[services.local_db]
public_source = false        # I control the data
secret_data = false          # no secrets
public_sink = false          # data stays local
dangerous_writes = true      # deletions need approval
```

### Per-workspace overrides

Workspaces can override global service declarations, but **only to set properties to `"forbidden"`**. This prevents accidentally relaxing security:

```toml
# Research workspace: email is read-only
[workspaces.research.services.email]
public_sink = "forbidden"
dangerous_writes = "forbidden"
```

The override validation is enforced at startup — any override that isn't `"forbidden"` is rejected.

### Workspace-level `contains_secrets`

Workspaces can declare that their local filesystem contains secrets (`.env` files, credentials, etc.):

```toml
[workspaces.personal]
contains_secrets = true
```

This is an explicit user declaration, NOT auto-derived from MCP services. Having access to a passwords MCP does not mean the local filesystem has secrets — those are separate contexts.

## Two Independent Taints

The system tracks two independent contamination flags per container invocation:

### Corruption taint

Set when the agent reads from a service with `public_source = true`. Indicates the agent MAY have been prompt-injected and its judgment cannot be trusted.

### Secret taint

Set when:
1. The agent calls an MCP service with `secret_data = true`, OR
2. The agent uses file-access tools (Read, Execute, Bash) AND the workspace has `contains_secrets = true`

Indicates the agent has secrets in its context that could be leaked through writes.

### Why two taints

| Corruption | Secret | Threat |
|---|---|---|
| no | no | Agent is trustworthy, holds no secrets. Low risk. |
| no | yes | Agent has secrets but isn't compromised. Just following user instructions. |
| yes | no | Agent may be hijacked but has no secrets to leak. Can vandalize but not exfiltrate. |
| yes | yes | **Full trifecta** (if public_sink exists). Maximum danger. |

Example: "Email my wife the secret journal entry." The agent reads the journal (`secret_taint = true`) and sends email (`public_sink = true`). But `corruption_taint = false` — the agent hasn't read any untrusted input. No deputy needed; just human confirmation (because `dangerous_writes = true` on email). The user approves, email goes through.

If that same agent had ALSO read a Reddit thread (`public_source = true`), THEN `corruption_taint = true` + `secret_taint = true` + `public_sink = true` = full trifecta = deputy + human gate.

### Taint lifecycle

- Both taints are **sticky** for the lifetime of a container invocation
- `/c` (clear context) restarts the container → fresh taint state (both cleared)
- After a tainted session ends, record which files it changed (for future deputy scanning of the diff before the next session starts — implemented in a later step)

## Gating Matrix

Two independent components determine the gating for write operations:

**Deputy review** — applied when `corruption_taint = true` (any write by a potentially-hijacked agent gets deputy scrutiny)

**Human confirmation** — applied when:
- `dangerous_writes = true` (always, regardless of taint), OR
- `corruption_taint = true` AND `secret_taint = true` AND `public_sink = true` (full trifecta)

### Full matrix

| corruption | secret | `public_sink` | `dangerous_writes` | Gating |
|---|---|---|---|---|
| no | * | false | false | **none** |
| no | * | false | true | **human** |
| no | * | true | false | **none** |
| no | * | true | true | **human** |
| yes | no | false | false | **deputy** |
| yes | no | false | true | **deputy + human** |
| yes | no | true | false | **deputy** |
| yes | no | true | true | **deputy + human** |
| yes | yes | false | false | **deputy** |
| yes | yes | false | true | **deputy + human** |
| yes | yes | true | false | **deputy + human** |
| yes | yes | true | true | **deputy + human** |
| — | — | forbidden | — | **blocked** |
| — | — | — | forbidden | **blocked** |

### Read gating

| `public_source` | Effect |
|---|---|
| `false` | No scanning, no corruption taint |
| `true` | Deputy scans content, container marked corruption-tainted |
| `"forbidden"` | Read blocked entirely |

## Code Changes

### Replace

- `security/middleware.py` — rewrite as `SecurityPolicy` (trust-based, two-taint-aware)
- `McpToolConfig`, `RateLimitConfig` in `types.py` — remove entirely
- `WorkspaceSecurity` in `types.py` — rebuild around `ServiceTrustConfig`

### New types in `types.py`

```python
# Tri-state: False (safe), True (risky/gated), "forbidden" (blocked)
TrustLevel = Literal[False, True, "forbidden"]

@dataclass
class ServiceTrustConfig:
    """Four properties per service — the user-facing security model."""
    public_source: TrustLevel = True
    secret_data: bool = True  # True/False only (forbidden doesn't apply)
    public_sink: TrustLevel = True
    dangerous_writes: TrustLevel = True

@dataclass
class WorkspaceSecurity:
    """Security configuration for a workspace."""
    services: dict[str, ServiceTrustConfig] = field(default_factory=dict)
    contains_secrets: bool = False  # explicit: local filesystem has secrets
```

### New `SecurityPolicy` in `security/middleware.py`

```python
class SecurityPolicy:
    """Single entry point for all security decisions per container invocation."""
    _services: dict[str, ServiceTrustConfig]
    _corruption_tainted: bool = False
    _secret_tainted: bool = False
    _workspace_contains_secrets: bool

    def evaluate_read(self, service: str) -> PolicyDecision
    def evaluate_write(self, service: str, data: dict) -> PolicyDecision
    def notify_file_access(self) -> None  # called when agent uses Read/Execute/Bash
```

### Update `_handlers_service.py`

- `_resolve_security` reads new TOML structure
- `_handle_service_request` calls `SecurityPolicy` instead of old `PolicyMiddleware`
- Policy is per-container-invocation (taint is per-invocation, not cached)

### Keep

- `security/audit.py` — update event fields (drop `tier`, add taint booleans)
- `security/mount_security.py` — unrelated
- `PolicyDeniedError` — still used by `group_queue.py`
- Response file writing, plugin dispatch, prefix registration

## Stubs

| Component | This step | Later step |
|-----------|-----------|------------|
| `ServiceTrustConfig` | Real | — |
| Two-taint tracking | Real | — |
| Decision matrix | Real | — |
| TOML config parsing + override validation | Real | — |
| Deputy scanning | **Stub** (always passes, logs) | Step 7 |
| Human approval gate | **Stub** (always passes, logs warning) | Step 6 |
| Poisoned worktree diff scan | **Stub** (noted, needs deputy) | Step 7 |

## References

- [The Lethal Trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — Simon Willison
- [AI Agent Security](https://simonwillison.net/2025/Jun/15/ai-agent-security/) — Simon Willison
- [Meta: Practical AI Agent Security](https://ai.meta.com/blog/practical-ai-agent-security/) — Agents Rule of Two
