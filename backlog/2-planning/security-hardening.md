# Security Hardening - Overview

## Summary

This project adds security layers to Pynchy, enabling agents to safely use external services without creating the conditions for prompt injection attacks.

**Status:** Core trust model implemented (Steps 1, 2, 4 done). Remaining: IPC narrowing, human approval gate, deputy agent.

## The Problem: The Lethal Trifecta

An agent becomes dangerous when it has all three of:
- **A) Untrusted input** — data from sources we don't control (Slack messages, web content)
- **B) Sensitive data** — information that would cause harm if leaked (corporate docs, credentials)
- **C) Untrusted sinks** — channels that could be used for exfiltration (sending messages, submitting forms)

Not every service contributes to the trifecta. A personal calendar is fully trusted. A corporate Slack has untrusted input, sensitive data, and is an untrusted sink.

## The Solution: Four Booleans Per Service

Each service declares four trust properties in `config.toml`:

```toml
[services.caldav]
public_source = false      # we control what's in it
secret_data = false         # calendar events aren't confidential
public_sink = false         # writing to our own calendar is safe
dangerous_writes = false    # calendar edits are reversible

[services.slack_mcp_acme]
public_source = true        # messages from others in the workspace
secret_data = true          # corporate conversations are confidential
public_sink = true          # can DM and post to channels
dangerous_writes = true     # sending messages is irreversible
```

Values: `false` (safe), `true` (risky — gated), `"forbidden"` (blocked entirely).

Per-workspace overrides mark workspaces containing sensitive data:

```toml
[sandbox.acme-1.security]
contains_secrets = true
```

See [docs/usage/security.md](../../docs/usage/security.md) for the full configuration guide.

## How It Works: Tainted Container Model

The runtime tracks two independent taint flags per container invocation:

- **`corruption_tainted`** — set when agent reads from a `public_source`
- **`secret_tainted`** — set when agent reads `secret_data` or accesses a workspace with `contains_secrets = true`

The gating matrix on writes:

| Condition | Gate |
|-----------|------|
| `dangerous_writes = "forbidden"` | **Blocked** |
| `dangerous_writes = true` | **Human approval** |
| corruption + secret + `public_sink` | **Human approval** (the trifecta) |
| corruption + `public_sink` | **Deputy review** |
| None of the above | **Allowed** |

A payload secrets scanner (`detect-secrets`) also runs on outbound writes, escalating to human approval if credential patterns are detected.

## Implementation Plan

### Completed

- **Step 1: Service Trust Profiles** → [5-completed/security-hardening-1-profiles.md](../5-completed/security-hardening-1-profiles.md)
  `ServiceTrustConfig`, `WorkspaceSecurity`, TOML config, DB serialization.

- **Step 2: Policy Middleware & Taint Tracking** → [5-completed/security-hardening-2-mcp-policy.md](../5-completed/security-hardening-2-mcp-policy.md)
  `SecurityPolicy` with two-taint model, audit logging, IPC handler integration, payload secrets scanner.

- **Step 4: Calendar Integration** → [5-completed/security-hardening-4-calendar.md](../5-completed/security-hardening-4-calendar.md)
  CalDAV adapter (pre-existing plugin), now configured with trust declarations.

### Remaining

#### [Step 0: Reduce IPC Surface](security-hardening-0-ipc-surface.md)
**Scope:** Signal-only IPC + inotify
**Dependencies:** None

#### [Step 6: Human Approval Gate](security-hardening-6-approval.md)
**Scope:** Approval flow for tainted containers writing to untrusted sinks
**Dependencies:** Steps 1-2 (done)

#### [Step 7: Deputy Agent (Input Filtering)](security-hardening-7-input-filter.md)
**Scope:** LLM-based content sanitization for untrusted sources
**Dependencies:** Steps 1-2 (done)

## Security Guarantees

1. **Credentials never in container** — all service credentials live only in the host process
2. **Agent cannot bypass gates** — policy enforcement runs in host, not in LLM
3. **Four booleans** — users configure `public_source`, `secret_data`, `public_sink`, `dangerous_writes` per service
4. **Two-taint tracking** — corruption taint (untrusted input) and secret taint (sensitive data) tracked independently
5. **Default deny** — unknown services default to all-true (maximum gating)
6. **Audit trail** — all policy evaluations recorded in existing `messages` table
7. **Payload scanning** — outbound writes scanned for credential patterns via detect-secrets

## References

- [The Lethal Trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — Simon Willison
- [AI Agent Security](https://simonwillison.net/2025/Jun/15/ai-agent-security/) — Simon Willison
- [CaMeL: Prompt Injection Mitigation](https://simonwillison.net/2025/Apr/11/camel/) — Simon Willison on Google DeepMind
- [Meta: Practical AI Agent Security](https://ai.meta.com/blog/practical-ai-agent-security/) — Agents Rule of Two
- [Design Patterns for Securing LLM Agents](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/)
- [MCP Prompt Injection](https://simonwillison.net/2025/Apr/9/mcp-prompt-injection/) — Tool shadowing, cross-server attacks
- [New Prompt Injection Papers](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/) — Rule of Two + Attacker Moves Second
