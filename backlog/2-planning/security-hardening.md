# Security Hardening - Overview

## Summary

This project adds comprehensive security layers to Pynchy, enabling multi-workspace agents with scoped privileges for sensitive operations (email, passwords, calendar, banking, etc.).

**Status:** Broken into 7 sub-plans (see below)

## The Problem: The Lethal Trifecta

The orchestrator (agent) has:
- **A) Untrusted input** (user messages, emails, web content)
- **B) Sensitive data** (passwords, banking info, personal calendar)
- **C) External communications** (email, WhatsApp, banking APIs)

Having all three is dangerous. We mitigate by **gating C** with deterministic host-side controls that the agent cannot bypass.

## The Solution: Layered Security

### Primary Defense: Action Gating (Steps 1, 2, 6)

Hard policy checks on tool execution:

| Tier | Gating | Examples |
|------|--------|---------|
| **Read-only** | Auto-approved | `read_email`, `list_calendar`, `bank_balance` |
| **Write** | Policy check (rules engine) | `create_event`, `update_task`, `archive_email` |
| **External / destructive** | Human approval via WhatsApp | `send_email`, `get_password`, `bank_transfer` |

The policy engine runs in the host process, independent of the agent. The agent cannot bypass these gates.

Rate limiting is enforced at the policy layer — even auto-approved tools have per-workspace, per-tool call limits to prevent a jailbroken agent from spamming read operations.

### Secondary Defense: Input Filtering (Step 7)

Optional defense-in-depth using a "Deputy Agent" that scans untrusted content for prompt injection before the orchestrator sees it. Catches obvious attacks; sophisticated attacks are caught by the action gate.

### Service Integrations (Steps 3-5)

Host-side adapters that execute MCP requests using real credentials:
- **Email** (IMAP/SMTP or Gmail API)
- **Calendar** (CalDAV or Google Calendar API)
- **Passwords** (1Password CLI)

Credentials live only in the host process, never exposed to containers or agents.

### Cross-Cutting: Audit Log & Non-Retryable Denials

Every policy evaluation is recorded in the existing `messages` table (`sender='security'`), prunable independently with `DELETE FROM messages WHERE sender = 'security' AND timestamp < cutoff`. No new tables needed. Policy denials are marked as non-retryable errors — the GroupQueue will not retry container runs that failed due to a deterministic policy denial.

### Post-Step 2: SecurityPolicy Facade

After Steps 1 and 2 are complete, the separate components (`WorkspaceSecurityProfile`, `PolicyMiddleware`, `ApprovalManager`, `DeputyAgent`) should be composed into a single `SecurityPolicy` object per workspace. This is the single entry point for all security decisions:

```python
class SecurityPolicy:
    """Single entry point for all security decisions for a workspace."""
    profile: WorkspaceSecurityProfile
    middleware: PolicyMiddleware
    approval_manager: ApprovalManager | None
    deputy: DeputyAgent | None

    async def evaluate_tool_call(self, tool_name, request) -> PolicyDecision:
        """Rate limit → tier evaluation → approval routing → audit log."""
```

This makes it easy to answer "what security governs workspace X?" and keeps the IPC watcher's integration clean.

## Implementation Plan

This project is broken into 8 sub-plans. The recommended order is **0 → 1 → 2 → 6 → 3/4/5 → 7**. Step 0 narrows the IPC surface before Steps 3-5 add more tools to it; Step 6 establishes the human approval gate before service integrations make it load-bearing.

### [Step 0: Reduce IPC Surface](security-hardening-0-ipc-surface.md)
**Scope:** Signal-only IPC + Deputy mediation + inotify
**Time:** 6-8 hours
**Dependencies:** None
**Must complete before:** Steps 3-5

Narrow the IPC pipe from arbitrary payloads to signals (Tier 1) and Deputy-mediated requests (Tier 2). Replace polling with inotify. This hardens the transport before service integrations add more tools on top of it.

### [Step 1: Workspace Security Profiles](security-hardening-1-profiles.md)
**Scope:** Config schema and validation for security profiles (including rate limits)
**Time:** 2-3 hours
**Dependencies:** None

Define the security model: which tools each workspace can access, their risk tiers, and rate limits. Pure configuration layer - no service integrations yet.

### [Step 2: MCP Tools & Basic Policy](security-hardening-2-mcp-policy.md)
**Scope:** New IPC MCP tools + policy middleware + audit log + rate limiting
**Time:** 5-7 hours
**Dependencies:** Step 1

Add MCP tools for email, calendar, passwords. Implement policy enforcement middleware with rate limiting, audit logging, and non-retryable denial classification. Tools return mock responses (real service integrations come in Steps 3-5).

### [Step 6: Human Approval Gate](security-hardening-6-approval.md)
**Scope:** WhatsApp approval flow for high-risk actions
**Time:** 5-6 hours
**Dependencies:** Steps 1-2

Implement the human approval system. When an agent attempts an EXTERNAL-tier action, the host sends an approval request via WhatsApp and waits for user response (approve/deny). Default: deny after 5-minute timeout.

**This is the primary security boundary. Complete before Steps 3-5 so service integrations are gated from day one.**

### [Step 3: Email Integration](security-hardening-3-email.md)
**Scope:** Host-side email adapter (IMAP/SMTP)
**Time:** 4-5 hours
**Dependencies:** Steps 0-2, 6

Implement email service adapter using IMAP/SMTP. Agents can read and send emails through the policy-gated IPC mechanism.

### [Step 4: Calendar Integration](security-hardening-4-calendar.md)
**Scope:** Host-side calendar adapter (CalDAV)
**Time:** 4-5 hours
**Dependencies:** Steps 0-2, 6

Implement calendar service adapter using CalDAV. Agents can list, create, and delete calendar events.

### [Step 5: Password Manager Integration](security-hardening-5-passwords.md)
**Scope:** Host-side 1Password CLI adapter
**Time:** 3-4 hours
**Dependencies:** Steps 0-2, 6

Implement password manager adapter using 1Password CLI. Agents can search and retrieve passwords (with human approval).

### [Step 7: Input Filtering (Optional)](security-hardening-7-input-filter.md)
**Scope:** Deputy Agent for prompt injection detection
**Time:** 4-5 hours
**Dependencies:** Steps 1-2, 6

Implement optional defense-in-depth input filtering using an LLM-as-judge pattern. Scans untrusted content (email bodies, web pages) for prompt injection before the orchestrator sees it.

**This is optional** - workspaces can disable it for performance or if they prefer human review only.

## Total Time Estimate

- **Steps 0-2, 6 (foundation):** 18-24 hours (2-3 full work days)
- **Steps 3-5 (service integrations):** 11-14 hours (1-2 full work days)
- **Step 7 (optional):** 4-5 hours additional

## How This Extends the Existing Pattern

Current Pynchy already works this way for messaging:

1. Agent calls `send_message` MCP tool
2. MCP server writes IPC file to `/workspace/ipc/output/`
3. Host reads IPC file
4. Host actually sends the WhatsApp message (agent never has WhatsApp creds)

We're extending this to email, passwords, calendar, banking. Same IPC mechanism, same trust model, more MCP tools, more policy enforcement.

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
│  │   IPC Watcher               │   │
│  │   (monitors output dir)     │   │
│  └───────────┬─────────────────┘   │
│              │                      │
│              ▼                      │
│  ┌─────────────────────────────┐   │
│  │   SecurityPolicy            │   │
│  │   ┌───────────────────┐    │   │
│  │   │ Rate Limiter      │    │   │
│  │   │ (sliding window)  │    │   │
│  │   └────────┬──────────┘    │   │
│  │            ▼               │   │
│  │   ┌───────────────────┐    │   │
│  │   │ Policy Middleware  │    │   │
│  │   │ (tier evaluation)  │    │   │
│  │   └────────┬──────────┘    │   │
│  │            │               │   │
│  │     ┌──────┴────────┐     │   │
│  │     ▼               ▼     │   │
│  │  Auto-approve  Human Gate │   │
│  │     │          (WhatsApp) │   │
│  │     └──────┬────────┘     │   │
│  │            ▼               │   │
│  │   ┌───────────────────┐    │   │
│  │   │ Audit Log         │    │   │
│  │   │ (messages table)  │    │   │
│  │   └───────────────────┘    │   │
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
│      (Gmail, iCloud, 1Password)     │
└─────────────────────────────────────┘
```

## Security Guarantees

1. **Credentials never in container** - All service credentials live only in the host process
2. **Agent cannot bypass gates** - Policy enforcement runs in host, not in LLM
3. **Default deny** - Unknown tools and expired approvals are denied by default
4. **Audit trail** - All policy evaluations recorded in existing `messages` table (`sender='security'`), independently prunable
5. **Workspace isolation** - Each workspace has independent security profile and rate limits
6. **Rate limiting** - Per-workspace, per-tool call limits prevent abuse even for auto-approved tools
7. **Non-retryable denials** - Policy denials are deterministic; the queue does not retry them

## Testing Strategy

Each step includes unit tests for:
- Configuration validation
- Policy evaluation logic
- Service adapter operations (mocked)
- Error handling and edge cases

Integration tests:
- End-to-end IPC flow with policy enforcement
- Approval timeout behavior
- Multi-workspace isolation

## Documentation Requirements

Each step must update:
- Setup instructions (if introducing new dependencies)
- Configuration examples
- Security best practices
- Troubleshooting guide

## Success Metrics

1. **Functional:** All 6 required steps implemented and tested
2. **Security:** No bypass possible (verified by penetration testing)
3. **Usability:** Approval flow is clear and responsive
4. **Performance:** Deputy agent (if enabled) adds < 1s per email
5. **Documentation:** Complete setup guide for all services

## References

- [The Lethal Trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — Simon Willison
- [AI Agent Security](https://simonwillison.net/2025/Jun/15/ai-agent-security/) — Simon Willison
- [CaMeL: Prompt Injection Mitigation](https://simonwillison.net/2025/Apr/11/camel/) — Simon Willison on Google DeepMind
- [Meta: Practical AI Agent Security](https://ai.meta.com/blog/practical-ai-agent-security/) — Agents Rule of Two
- [Design Patterns for Securing LLM Agents](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/) — Code-then-Execute, Context Minimization
- [MCP Prompt Injection](https://simonwillison.net/2025/Apr/9/mcp-prompt-injection/) — Tool shadowing, cross-server attacks
- [New Prompt Injection Papers](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/) — Rule of Two + Attacker Moves Second

## Related Work

- Plugin Hook system (similar breakdown into sequential steps)
- Current IPC MCP system (foundation for this work)
- Message types refactor (clean message handling)
