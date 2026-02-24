# Security Hardening - Overview

## Summary

This project adds security layers to Pynchy, enabling agents to safely use external services (email, passwords, calendar, etc.) without creating the conditions for prompt injection attacks.

**Status:** Broken into 7 sub-plans (see below)

## The Problem: The Lethal Trifecta

An agent becomes dangerous when it has all three of:
- **A) Untrusted input** — data from sources we don't control (emails from strangers, web content)
- **B) Sensitive data** — information that could cause harm if leaked (passwords, banking info)
- **C) Untrusted sinks** — channels that could be used for exfiltration or harm (sending emails, external APIs)

But **not every service contributes to the trifecta**. A personal calendar is fully trusted — we control the data, it's not sensitive, and writing to it is safe. Email, on the other hand, has untrusted input (incoming mail from strangers) and is an untrusted sink (can send to anyone).

## The Solution: Four Booleans Per Service

> **Updated 2026-02-24:** Implemented with four properties instead of three. The original `trusted_source`/`sensitive_info`/`trusted_sink` model was refined into `public_source`/`secret_data`/`public_sink`/`dangerous_writes` with a tri-state (False/True/"forbidden"). See `docs/plans/2026-02-23-lethal-trifecta-defenses-design.md`.

Each service declares four trust properties:

```toml
[services.calendar]
public_source = false      # we control what's in it
secret_data = false        # calendar events aren't secrets
public_sink = false        # writing to our own calendar is safe
dangerous_writes = false   # calendar writes are reversible

[services.email]
public_source = true       # incoming mail is untrusted
secret_data = false        # email content isn't inherently secret
public_sink = true         # can send to anyone — exfiltration vector
dangerous_writes = true    # sending email is irreversible

[services.passwords]
public_source = false      # vault data is trusted
secret_data = true         # passwords ARE secrets
public_sink = false        # not a send channel
dangerous_writes = "forbidden"  # never allow password writes
```

These can be applied at any granularity:
- **Per service instance** — a specific CalDAV address, a specific IMAP account
- **Per tool** — mark the web search tool as `public_source = true`

This is all a user needs to understand to keep their system secure. Four booleans per service.

## How It Works: Tainted Container Model

The runtime tracks whether a container has been **tainted** by untrusted input:

1. **Reads from untrusted source (`trusted_source = false`):**
   - Content is sanitized by a **deputy agent** (fresh context, no tools) before the orchestrator sees it
   - The container is **marked as tainted**

2. **Tainted container tries to write to untrusted sink (`trusted_sink = false`):**
   - A deputy reviews the outbound content
   - A **human gate** is triggered (approval via WhatsApp)
   - Both must pass before the action proceeds

3. **Tainted container accesses sensitive data (`sensitive_info = true`):**
   - Human gate triggered — the combination of tainted + sensitive is dangerous

4. **Fully trusted services (`trusted_source = true, sensitive_info = false, trusted_sink = true`):**
   - **No gating, no deputy, no overhead.** Execute unfettered.

Rate limiting applies to all services regardless of trust declarations, to prevent runaway loops.

## Service Trust Examples

| Service | trusted_source | sensitive_info | trusted_sink | Result |
|---------|---------------|----------------|-------------|--------|
| Personal calendar | true | false | true | No gating |
| Email (IMAP/SMTP) | false | false | false | Deputy on reads, taint + human gate on sends |
| Password manager | true | true | false | Human gate when tainted container requests password |
| Web browsing | false | false | N/A | Deputy on content, taints container |
| Personal Nextcloud | true | false | true | No gating |
| Shared calendar (untrusted participants) | false | false | true | Deputy on reads, taints container |

## Service Integrations (Steps 3-5)

Host-side adapters that execute IPC requests using real credentials:
- **Email** (IMAP/SMTP) — `trusted_source: false, sensitive_info: false, trusted_sink: false`
- **Calendar** (CalDAV) — `trusted_source: true, sensitive_info: false, trusted_sink: true`
- **Passwords** (1Password CLI) — `trusted_source: true, sensitive_info: true, trusted_sink: false`

Credentials live only in the host process, never exposed to containers or agents.

## Cross-Cutting: Audit Log & Non-Retryable Denials

Every policy evaluation is recorded in the existing `messages` table (`sender='security'`), prunable independently with `DELETE FROM messages WHERE sender = 'security' AND timestamp < cutoff`. No new tables needed. Policy denials are marked as non-retryable errors — the GroupQueue will not retry container runs that failed due to a deterministic policy denial.

## SecurityPolicy Facade

The components compose into a single `SecurityPolicy` object per workspace:

```python
class SecurityPolicy:
    """Single entry point for all security decisions for a workspace."""
    service_trust: dict[str, ServiceTrustConfig]  # per-service trust declarations
    tainted: bool  # has this container seen untrusted input?
    deputy: DeputyAgent | None
    approval_manager: ApprovalManager | None

    def is_tainted(self) -> bool:
        """Has the container read from an untrusted source?"""
        return self.tainted

    async def evaluate_read(self, service_name, data) -> PolicyDecision:
        """Evaluate a read operation. Deputy scans if untrusted source, taints container."""

    async def evaluate_write(self, service_name, data) -> PolicyDecision:
        """Evaluate a write operation. Human gate if tainted + untrusted sink."""

    async def evaluate_access(self, service_name) -> PolicyDecision:
        """Evaluate sensitive data access. Human gate if tainted + sensitive."""
```

## Implementation Plan

This project is broken into 8 sub-plans. The recommended order is **0 → 1 → 2 → 6 → 3/4/5 → 7**.

### [Step 0: Reduce IPC Surface](security-hardening-0-ipc-surface.md)
**Scope:** Signal-only IPC + inotify
**Dependencies:** None

### [Step 1: Service Trust Profiles](security-hardening-1-profiles.md)
**Scope:** `ServiceTrustConfig` schema, per-workspace configuration, taint tracking
**Dependencies:** None

### [Step 2: Policy Middleware & Taint Tracking](security-hardening-2-mcp-policy.md)
**Scope:** IPC MCP tools + taint-aware policy middleware + audit log + rate limiting
**Dependencies:** Step 1

### [Step 6: Human Approval Gate](security-hardening-6-approval.md)
**Scope:** WhatsApp approval flow for tainted containers writing to untrusted sinks
**Dependencies:** Steps 1-2

### [Step 3: Email Integration](security-hardening-3-email.md)
**Scope:** Host-side email adapter (IMAP/SMTP)
**Dependencies:** Steps 0-2, 6

### [Step 4: Calendar Integration](security-hardening-4-calendar.md)
**Scope:** Host-side calendar adapter (CalDAV). Fully trusted — no gating needed.
**Dependencies:** Steps 0-2

### [Step 5: Password Manager Integration](security-hardening-5-passwords.md)
**Scope:** Host-side 1Password CLI adapter
**Dependencies:** Steps 0-2, 6

### [Step 7: Deputy Agent (Input Filtering)](security-hardening-7-input-filter.md)
**Scope:** LLM-based content sanitization for untrusted sources
**Dependencies:** Steps 1-2

## Architecture

```
┌─────────────────────────────────────┐
│   Container (Agent + MCP Server)    │
│                                     │
│  ┌─────────────────────────────┐   │
│  │   Orchestrator Agent        │   │
│  │   (Claude Agent SDK)        │   │
│  └───────────┬─────────────────┘   │
│              │ Tool calls          │
│              ▼                      │
│  ┌─────────────────────────────┐   │
│  │   IPC MCP Server            │   │
│  │   (read_email, send_email,  │   │
│  │    get_password, etc.)      │   │
│  └───────────┬─────────────────┘   │
│              │ Write IPC files     │
└──────────────┼─────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│        Host Process                 │
│                                     │
│  ┌─────────────────────────────┐   │
│  │   IPC Watcher (inotify)     │   │
│  └───────────┬─────────────────┘   │
│              │                      │
│              ▼                      │
│  ┌─────────────────────────────┐   │
│  │   SecurityPolicy            │   │
│  │                             │   │
│  │   1. Rate limiter           │   │
│  │   2. Trust check            │   │
│  │      ├─ trusted service     │   │
│  │      │  → pass through      │   │
│  │      ├─ untrusted source    │   │
│  │      │  → deputy scan       │   │
│  │      │  → mark tainted      │   │
│  │      └─ tainted + untrusted │   │
│  │         sink/sensitive       │   │
│  │         → deputy + human    │   │
│  │           gate              │   │
│  │   3. Audit log              │   │
│  └───────────┬─────────────────┘   │
│              │                      │
│              ▼                      │
│  ┌─────────────────────────────┐   │
│  │   Service Adapters          │   │
│  │   - Email (IMAP/SMTP)       │   │
│  │   - Calendar (CalDAV)       │   │
│  │   - Passwords (1Password)   │   │
│  └───────────┬─────────────────┘   │
│              │                      │
│              ▼                      │
│      External Services              │
│      (Gmail, Nextcloud, 1Password)  │
└─────────────────────────────────────┘
```

## Security Guarantees

1. **Credentials never in container** - All service credentials live only in the host process
2. **Agent cannot bypass gates** - Policy enforcement runs in host, not in LLM
3. **Three booleans** - Users configure `trusted_source`, `sensitive_info`, `trusted_sink` per service. That's all they need to understand
4. **Taint tracking** - Containers that read untrusted content are marked tainted; tainted containers face gating on untrusted sinks and sensitive data
5. **Default deny** - Unknown services default to `{trusted_source: false, sensitive_info: true, trusted_sink: false}` (maximum gating)
6. **Audit trail** - All policy evaluations recorded in existing `messages` table
7. **Rate limiting** - Per-workspace, per-tool call limits prevent abuse even for fully trusted services
8. **Non-retryable denials** - Policy denials are deterministic; the queue does not retry them

## References

- [The Lethal Trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — Simon Willison
- [AI Agent Security](https://simonwillison.net/2025/Jun/15/ai-agent-security/) — Simon Willison
- [CaMeL: Prompt Injection Mitigation](https://simonwillison.net/2025/Apr/11/camel/) — Simon Willison on Google DeepMind
- [Meta: Practical AI Agent Security](https://ai.meta.com/blog/practical-ai-agent-security/) — Agents Rule of Two
- [Design Patterns for Securing LLM Agents](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/)
- [MCP Prompt Injection](https://simonwillison.net/2025/Apr/9/mcp-prompt-injection/) — Tool shadowing, cross-server attacks
- [New Prompt Injection Papers](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/) — Rule of Two + Attacker Moves Second
