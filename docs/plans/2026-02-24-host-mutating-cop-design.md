# Host-Mutating Operations & the Cop

## Problem

The lethal trifecta model protects against **data exfiltration** — a prompt-injected agent leaking secrets through public sinks. But there's an orthogonal threat class: **host code execution**. If an adversary hijacks a container, they don't need to exfiltrate data — they can merge malicious code, trigger deployments, or register persistent backdoors.

The current model has two blind spots:

1. **Host-mutating operations have no Cop oversight.** IPC handlers for `sync_worktree_to_main`, `deploy`, `register_group`, `schedule_task`, etc. check `is_admin` but don't inspect content. A prompt-injected admin container can execute these unchecked.

2. **The admin channel has no corruption protection.** Admin containers can read from `public_source=true` MCPs (web browsers, email). If the admin agent gets prompt-injected, it has full access to host-mutating operations with zero gating.

### Two orthogonal threat classes

| Threat class | Kill chain | Defense |
|---|---|---|
| **Data exfiltration** (trifecta) | Injection → secrets in context → leak via public sink | Taint tracking + approval gate |
| **Host code execution** (new) | Injection → merge/schedule/register malicious code | Admin clean room + Cop inspection |

## Solution

Three new defenses, layered:

1. **Admin clean room** — prevent corruption at the source
2. **Host-mutating classification** — identify operations that affect the host
3. **Cop dual-inspection** — automated review at both ends of the pipeline

### 1. Admin clean room

The admin channel becomes a trusted execution environment by forbidding all `public_source=true` MCPs. If the admin agent never reads untrusted content, it can never become corruption-tainted, eliminating prompt injection as a threat vector for the most privileged workspace.

**Enforcement:** Config validation at startup. Reject any admin workspace configuration where an assigned MCP has `public_source=true` (or defaults to `true` as an unknown service). This is a startup error, not a runtime check.

**Consequence:** Admin can't browse the web, read emails, or access any service with untrusted input. For those tasks, create a non-admin workspace with appropriate gating.

**Cross-group contamination:** When content flows from a tainted non-admin container to the admin (e.g., cross-group messaging, reading output), the Cop mediates — inspecting the content before the admin agent sees it. This preserves the clean room property even when interacting with tainted workspaces.

### 2. Host-mutating operations

A new classification orthogonal to the four service trust properties. An operation is **host-mutating** if it can change what code runs on the host machine.

**Hardcoded host-mutating IPC operations:**

| IPC Operation | What it mutates | Cop inspects |
|---|---|---|
| `sync_worktree_to_main` | Merges code into main branch | The git diff |
| `create_pr` | Creates PR from worktree | The git diff |
| `register_group` | Creates new workspace | Group config |
| `create_periodic_agent` | Creates persistent agent | Agent prompt/config |
| `schedule_task` | Schedules future execution | Task prompt |

**Auto-classified:** Script-type MCP servers (`type = "script"` in config.toml) run as host subprocesses. Any tool call is implicitly host-mutating. The Cop reviews tool arguments.

**Not host-mutating:**
- Docker-type MCPs (isolated in their own container)
- URL-type MCPs (remote, no host access)
- `deploy` (just restarts with whatever code is already on main — harmless if merges are gated)

**Escalation rule:**

```
needs_cop   = true          (always — defense in depth)
needs_human = cop_flagged
```

The Cop always reviews. Human only gets involved when the Cop detects something suspicious. No escalation based on taint state — that's the trifecta model's domain.

### 3. Cop dual-inspection

The Cop (formerly "Deputy") inspects at two points in the pipeline:

**Inbound inspection** (extends Step 7 scope):
- Content from `public_source=true` services → Cop scans for injection patterns before the agent sees it
- Content from tainted containers flowing to the admin → Cop mediates before it enters the admin context

**Outbound inspection** (new scope):
- All host-mutating operations → Cop reviews the payload (diff, prompt, config) for signs of manipulation

These are **asymmetric inspections**. The inbound Cop looks for injection *patterns* (instruction-like phrasing, escaped JSON, command separators). The outbound Cop looks for *anomalous actions* (unexpected diff content, suspicious task prompts, unusual group configs). An attacker would need to evade both — two fundamentally different evasion problems.

### Unified escalation model

| Operation type | Cop runs when | Human escalation trigger |
|---|---|---|
| **Sink writes** (trifecta) | `corruption_tainted` | Trifecta conditions OR `dangerous_writes` OR secrets scanner OR `cop_flagged` |
| **Host-mutating** (new) | Always | `cop_flagged` only |

### Playwright site whitelisting

For browser-type MCPs, a per-workspace `allowed_hosts` config bounds the corruption surface:

```toml
[workspaces.research.mcp.playwright]
allowed_hosts = "github.com;docs.python.org;stackoverflow.com"
```

The Cop should verify that navigation targets match the allowlist, not just trust the MCP server to enforce it host-side.

## Defense layers

```
Layer 0: Admin clean room — prevent corruption at the source
Layer 1: Cop inbound inspection — catch injection before the agent sees it
Layer 2: Cop outbound inspection — catch manipulation in host-mutating payloads
Layer 3: Human approval — only when automated review flags something
Layer 4: Container isolation — blast radius limit if all else fails
```

For admin, Layer 0 makes Layers 1–3 nearly silent. Strong security without approval fatigue.

## Relationship to existing security hardening

This design extends the existing sequence:

- **Step 0** (denied) envisioned Cop mediation for Tier 2 IPC requests — this design realizes that vision
- **Step 6** (human approval gate) remains the primary defense for trifecta scenarios — unchanged
- **Step 6.1** (Cop stub) becomes the implementation vehicle for both inbound and outbound inspection
- **Step 7** (input filtering) is subsumed — the Cop's inbound inspection is Step 7, and its outbound inspection for host-mutating ops is the new scope

New backlog items from this design:

| Item | Depends on |
|---|---|
| Admin clean room (config validation) | Nothing — can ship immediately |
| Host-mutating IPC classification + Cop gate | Cop implementation (Step 6.1) |
| Cop inbound inspection | Cop implementation (Step 6.1) |
| Cop outbound inspection | Cop implementation (Step 6.1) |
| Playwright allowlist enforcement | Nothing — can ship independently |
| Cross-group Cop mediation for admin | Cop implementation (Step 6.1) |
| Update security docs | After implementation — update `docs/architecture/security.md` (§5, §7, rename Deputy→Cop, add host-mutating section) and `docs/usage/security.md` (add admin clean room guidance, host-mutating explanation) |

## References

- [The Lethal Trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — Simon Willison
- [Meta: Practical AI Agent Security](https://ai.meta.com/blog/practical-ai-agent-security/) — Agents Rule of Two
- [CaMeL: Prompt Injection Mitigation](https://simonwillison.net/2025/Apr/11/camel/) — Google DeepMind
- [Design Patterns for Securing LLM Agents](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/)
- Existing design: [Lethal Trifecta Defenses](2026-02-23-lethal-trifecta-defenses-design.md)
- Existing design: [Human Approval Gate](2026-02-24-human-approval-gate-design.md)
