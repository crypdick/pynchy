# NanoClawPy: Architecture Design — Port + Security Isolation

## Context

We want to port NanoClaw from TypeScript to Python and then add a security isolation layer for multi-workspace agents with scoped privileges (banking, passwords, email, calendar, etc.).

Two separate efforts:

1. **The Port** — Faithful reproduction of the TypeScript behavior in Python (follows `docs/PYTHON_PORT_ROADMAP.md` exactly)
2. **The Security Layer** — Post-port enhancement: MCP-based privilege boundaries, action gating, input filtering

Port first, validate it works, then layer security on top.

---

## Decision 1: Claude Code SDK, not LangChain

The policy engine and MCP servers run in the host process, independent of the agent SDK. LangChain adds complexity for zero security gain. Stay with `claude-code-sdk`.

---

## Decision 2: MCP Servers as the Privilege Boundary

External systems (email, calendar, banking) are MCP servers. The agent calls the MCP, and the MCP abstracts away both A) the complexity of the external system, B) whether the output of the MCP ought to be sanitized or rejected. (via a specialist "policy agent" AKA the Deputy Agent that detects malicious prompt injection), C) whether to escalate the request to a human for approval

The MCP may be LLM-based, or just dumb code. Each MCP will probably run in an ephemeral container.

Different workspaces may have different MCP servers available to them.

A single MCP should not ever violate the principle of least privilege or the lethal trifecta.

---

### Lethal Trifecta Analysis

The orchestrator technically has A+B+C (untrusted input + sensitive data + external comms). But **C is gated** by the host. There can be deterministic human approval rules for specific MCPs, such as `bank_transfer`. This gate is in the host process — the LLM cannot bypass it. It's not AI-based detection; it's a hard policy check.

The input filter (prompt injection detection) adds defense-in-depth. If a sophisticated attack gets past the filter, the action gate catches it when the compromised orchestrator tries to exfiltrate via a high-risk MCP tool.

### Risk Tiers for MCP Tools

| Tier | Gating | Examples |
|------|--------|---------|
| **Read-only** | Auto-approved | `read_email`, `list_calendar`, `list_tasks`, `search_web`, `bank_balance` |
| **Write** | Policy check (rules engine) | `create_event`, `update_task`, `archive_email` |
| **External / destructive** | Human approval via WhatsApp | `send_email`, `get_password`, `bank_transfer`, `delete_email` |

The "policy check" tier uses a simple rules engine (not an LLM): e.g., "create_event is OK if the calendar is the user's own." This is deterministic.

### How This Extends the Existing Pattern

Current NanoClaw already works this way for messaging:

1. Agent calls `send_message` MCP tool
2. MCP server writes IPC file to `/workspace/ipc/output/`
3. Host reads IPC file
4. Host actually sends the WhatsApp message (agent never has WhatsApp creds)

We're just extending this to email, passwords, calendar, banking. Same IPC mechanism, same trust model, more MCP tools.

### Human Approval Flow

```
[APPROVAL REQUIRED]
Workspace: personal
Action: send_email(to="alice@example.com", subject="Meeting tomorrow")
Reply 'approve abc123' or 'deny abc123'
```

Sent via WhatsApp (or whatever active channel). Default: deny on timeout (5 min).

---

## Phase B: Security Layer (Post-Port)

Each step is independently useful. Ship incrementally.

### B.1: Workspace Security Profiles

Update the workspace settings to include:

- Which MCP tools each workspace gets
- Risk tier per tool (auto-approve / policy-check / human-approval)
- Startup validation: reject invalid configs

### B.2: New MCP Tools

Extend the container's MCP server (`ipc_mcp.py`) with new tools:

- `read_email`, `send_email`
- `get_password`
- `list_calendar`, `create_event`, `delete_event`
- etc. (one MCP tool per operation)

Each tool writes an IPC request file. The host processes it.

### B.3: Host-Side Service Integrations

The host gains adapters that execute MCP requests using real credentials:

- Email adapter (IMAP/SMTP or Gmail API)
- 1Password adapter (CLI wrapper)
- Calendar adapter (CalDAV or Google Calendar API)

Credentials live in the MCP server's container only.

### B.4: Policy Engine + Action Gating

`policy/engine.py` — evaluates every IPC request against the workspace profile:

- Look up the tool's risk tier
- Run rules engine for write tier
- Optionally use LLM-as-judget ("the Deputy Agent") to detect malicious prompt injection
- Send human approval request for high-risk tier

Wired into the IPC watcher as middleware.

### B.5: Input Filtering

`policy/deputy_agent.py` — optional detection layer for untrusted content:

- Run email bodies, web page content through a prompt injection detector before the orchestrator sees them
- Not a sole defense — defense-in-depth alongside action gating
- Catches obvious attacks (mass phishing, generic injection)
- Sophisticated adaptive attacks get caught by the action gate instead

### B.6: Human Approval Gate

`policy/approval.py` — sends approval requests via active channel:

- WhatsApp message with action description
- Timeout → default deny (5 min)
- Approve/deny via reply

**Starting posture: all cross-system writes and external communications require manual approval.** Relax later based on observed patterns.

---

## Sources

- [The Lethal Trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — Simon Willison
- [CaMeL: Prompt Injection Mitigation](https://simonwillison.net/2025/Apr/11/camel/) — Simon Willison on Google DeepMind
- [AI Agent Security](https://simonwillison.net/2025/Jun/15/ai-agent-security/) — Simon Willison
- [New Prompt Injection Papers](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/) — Rule of Two + Attacker Moves Second
- [Design Patterns for Securing LLM Agents](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/) — Code-then-Execute, Context Minimization, Map-Reduce
- [MCP Prompt Injection](https://simonwillison.net/2025/Apr/9/mcp-prompt-injection/) — Tool shadowing, cross-server attacks
- [The Summer of Johann](https://simonwillison.net/2025/Aug/15/the-summer-of-johann/) — Real-world prompt injection
- [Meta: Practical AI Agent Security](https://ai.meta.com/blog/practical-ai-agent-security/) — Agents Rule of Two
