# gVisor — Declined

**Date:** 2025-02-12
**Status:** Declined
**Category:** Container runtime / sandboxing

## What is gVisor?

gVisor (`runsc`) is an OCI-compatible container runtime that replaces `runc` with a userspace kernel written in Go. It intercepts syscalls at the container boundary, preventing them from reaching the host kernel directly. Used by Google Cloud Run, DigitalOcean App Platform, and other multi-tenant platforms.

## Why it was considered

Agents can be prompt-injected via untrusted input (emails, web content, calendar invites). A compromised agent could attempt container escape to access host resources. gVisor adds a syscall-filtering layer that would make container escapes harder.

## Why it was declined

### 1. Linux-only — incompatible with primary platform

gVisor requires Linux kernel 4.14.77+. It does not run on macOS. Pynchy's primary runtime is Apple Container on macOS, with Docker as a Linux/fallback option. Adopting gVisor would split the runtime story without covering the main platform.

### 2. Wrong layer for the actual threat

Prompt injection attacks operate **within** the container's authorized capabilities — the agent uses its legitimate MCP tools (send_message, schedule_task, etc.) to do harm. gVisor can't distinguish a prompt-injected `send_message` call from a legitimate one because both look identical at the syscall level.

| Threat | gVisor helps? | Application-layer security helps? |
|--------|:---:|:---:|
| Injected agent exfiltrates data via `send_message` | No | Yes — action gating |
| Injected agent reads credentials via authorized tool | No | Yes — risk-tiered approval |
| Agent escapes container to read host filesystem | Yes | No (but existing container isolation covers this) |
| Poisoned email tricks agent into forwarding secrets | No | Yes — input filtering + action gate |

The threat that gVisor addresses (container escape) is already mitigated by standard container isolation. The threat that actually matters (authorized tool misuse via prompt injection) requires application-layer defenses — see `docs/backlog/3-ready/security-hardening.md`.

### 3. Performance cost with no security payoff

gVisor intercepts every syscall in userspace. Pynchy agents run syscall-heavy workloads (Node.js, Python, Chromium, ripgrep). The gVisor docs warn of "poor performance for system call heavy workloads." This would degrade agent responsiveness for marginal security benefit.

## What we're doing instead

The security hardening plan (`security-hardening.md`) targets prompt injection at the right layer:

- **MCP privilege boundaries** — agents only get tools their workspace allows
- **Risk-tiered action gating** — deterministic policy the LLM can't bypass
- **Human approval** — hard gate for destructive/external actions
- **Input filtering** — deputy agent catches injection before the orchestrator sees it
- **Lethal trifecta mitigation** — no single component holds untrusted input + sensitive data + external comms without a gate
