# Comparison to ZeroClaw

## Conclusion

ZeroClaw is a broad standalone agent runtime. Pynchy is a messaging,
workspace, plugin, security-semantics, and durable-work product that delegates
model execution to mature cores behind LiteLLM. Pynchy should not pursue
feature parity or import ZeroClaw's agent loop, provider stack, SOP scheduler,
or monolithic control plane.

The high-value adaptations are contracts rather than adapter count:

1. Fail-closed control-plane networking and authenticated remote
   administration.
2. Typed, machine-readable plugin and capability descriptors that generate
   operational truth and documentation.
3. A read-only operator CLI for diagnostics, validation, initialization, and
   stable status output.
4. Composable channel capabilities and a durable media/attachment outbox.
5. Context-propagated execution attribution and deterministic replay.

Event-triggered workflows and core-neutral delegation are also useful, but
both belong on Temporal rather than a parallel ZeroClaw-style scheduler.

## Scope and evidence

This source-level assessment compares Pynchy `main` at
`682cef573137d053e5ffe13fe75a1fc89d6855b1` with ZeroClaw at
[`e592a555d69c6a701c0fa0fa3f94a4bbcffbb2c2`](https://github.com/zeroclaw-labs/zeroclaw/tree/e592a555d69c6a701c0fa0fa3f94a4bbcffbb2c2).
It distinguishes shipped defaults from optional features, partial scaffolding,
and roadmap claims.

Pynchy's baseline remains intentional: full agent cores run in per-workspace
Docker or Apple Container boundaries; LiteLLM owns provider breadth and
credential isolation; host-mediated tools receive semantic trust and approval
policy; Temporal owns durable schedules and recovery; ActionSpecs and canaries
provide independent semantic evidence.

## Architecture and capability comparison

| Area | Pynchy today | ZeroClaw today | Direction |
| --- | --- | --- | --- |
| Agent execution | Pluggable mature cores in per-workspace containers | Rust agent loop, named agents, subagents, runtime model switching | Preserve Pynchy's core boundary; add core-neutral delegation records, not another loop. |
| Providers | LiteLLM routing, budgets, rate limits, credential isolation | Direct provider slots and runtime routing | Do not duplicate LiteLLM with direct provider ownership. |
| Channels | Channel plugins, shared inbound audio transcription, text-centric outbound events | Thirty-plus adapters with uneven shipped support | Do not chase count. Build media/attachment primitives and add channels for real workflows. |
| Control plane | Fail-closed HTTP diagnostics and deployment over loopback TCP plus a permission-restricted Unix socket | Loopback default, public-bind and remote-admin opt-ins, pairing-derived auth, local IPC | Pynchy now has the local and remote-authentication foundation; pairing remains a possible future enhancement. |
| Plugins | Pluggy hooks; trusted Python entry points import into the host; several hooks return raw dictionaries | Permission manifests and WASM contracts, though disabled by default and not in release binaries | Add typed manifests and out-of-process execution for untrusted extensions; retain in-process Pluggy only for trusted code. |
| Tools and MCP | Host-owned IPC, on-demand MCP containers, workspace grants, action coverage, canaries | Native tools, MCP, feature gates, deferred tool loading | Pynchy already has stronger semantic evidence. Consider deferred schemas only after prompt-budget measurement. |
| Security | Container isolation, host-held credentials, source/secret/sink taint policy, clean-room admin policy | Command and path policy, pairing, OS sandboxing, OTP/estop, receipts | Preserve semantic policy; add control-plane authentication and durable action receipts. |
| Scheduling | Temporal schedules, interrupted-turn recovery, host jobs, managed worktree merges | Cron, heartbeat, routines, SOP steps | Keep Temporal as the only durable scheduler. Adapt event fan-in and typed step contracts. |
| Delegation | Isolated scheduled workspaces but no generic agent-callable handoff | Same-identity subagents, cross-agent delegation, fan-out | Add bounded same-profile delegation with immutable capability snapshots and Temporal cancellation/results. |
| Memory and history | SQLite FTS/BM25, explicit Obsidian learning, selective skills | Multiple stores, embeddings, graph/decay/dedup/export | Add provenance, budgets, conflicts, export, and audit before evaluating embeddings. |
| Observability | Structlog, EventBus, SQLite summaries, Phoenix, status, canaries | Typed logs, inherited context, rolling JSONL, dashboard bridge | Add a typed execution-attribution envelope; do not duplicate sensitive Phoenix bodies. |
| Evaluation | Hermetic tests, `pytest --action-coverage`, live canaries | Replay crate; live mode remains unfinished | Borrow replay fixtures while retaining canaries as independent production evidence. |
| Operator UX | Minimal CLI and hand-maintained configuration docs | Doctor, config operations, schema surfaces, stable errors | Build read-only diagnostics and generated truth before a dashboard. |

## Priority roadmap

### Implemented foundation: fail closed at the HTTP control plane

The HTTP surface includes status, capabilities, action/work-item records,
canaries, webhooks, and deployment. A network perimeter alone must not be its
only protection.

- Bind HTTP to loopback by default.
- Prefer a permission-restricted Unix socket for local CLI control,
  while retaining TCP loopback as the portable fallback.
- Require bootstrap-derived bearer authentication for every remote diagnostic
  or deployment call.
- Separate `allow_public_bind` from `allow_remote_deploy`; require
  authentication for either remote posture.
- Refuse public startup without authentication, rate-limit requests, and emit
  audit events.
- Keep readiness endpoints separately scoped and free of sensitive config.

This is an immediate security correction, not merely a product improvement.

Pynchy now ships the complete foundation above: loopback TCP and a mode-`0600`
Unix socket by default, explicit public-bind and remote-deploy gates, bootstrap-derived
bearer credentials, fail-closed startup, per-client rate limiting, durable security
audit events, and a separately scoped non-sensitive readiness response. See
[Control Plane Access](../../docs/usage/control-plane.md).

### Implemented foundation: host-action capability truth

Pynchy now parses service handlers into owned descriptors, validates their
security and evidence contracts, resolves workspace-specific capability
status, and exposes the result through `pynchy doctor`, `/capabilities`, and
`/status`. See
[Action coverage](../../docs/architecture/action-coverage.md#host-action-descriptors-and-capability-status).
The [OpenClaw comparison](comparison-to-openclaw.md#p1-expand-descriptor-coverage-and-add-pre-import-plugin-metadata)
tracks the remaining non-service plugin descriptors and pre-import metadata.

### P1: Expand operator diagnostics and configuration tooling

Generate configuration references from Pydantic schemas rather than
maintaining parallel lists. Expand `pynchy doctor` beyond host actions to cover
runtime, container, Temporal, LiteLLM, tunnel, channel, credential-presence,
plugin, and migration checks with remediation. Add:

- `pynchy config validate [--json]` to parse desired state without starting
  services;
- `pynchy init` that never echoes or persists secret values into tracked files;
- a stable `pynchy status --json` schema over the existing collector; and
- generated JSON Schema/OpenAPI plus stable machine-readable errors.

Do not copy ZeroClaw's configuration monolith to obtain these outcomes.

### P1: portable capabilities and orchestration

#### Compose channel capabilities and add a media/outbox contract

Replace optional behaviour discovered through `hasattr` with small,
runtime-checkable protocols such as `TypingChannel`, `StreamingChannel`,
`ReactionChannel`, `HistoryChannel`, `ProvisioningChannel`, and `MediaChannel`.
Do not create a giant default-heavy channel trait.

Add typed `Attachment`, `MediaKind`, `DeliveryIntent`, `ProviderReceipt`, and
delivery-state records. Enforce staging-path containment, size and type limits,
channel acceptance, redaction, retry/expiry, durable cleanup, and unknown
outcomes. Outbound TTS, generated images, files, and richer questions become
consumers of this contract rather than channel-specific branches.

#### Build event-triggered flows on Temporal

Adapt declarative steps, approvals, retry, conditions, and event fan-in as
typed Temporal workflow inputs and activities. Use deterministic event IDs,
source authentication, idempotency, and immutable policy snapshots. Start with
authenticated webhooks and filesystem/channel events; do not add a competing
SOP scheduler.

#### Add bounded core-neutral delegation

Expose an agent-callable delegation/handoff action that creates an isolated
child session and result artifact with cancellation and parent delivery. The
first release stays within one workspace/profile, inherits the intersection of
caller and target capabilities, and shares cost/action budgets. Cross-workspace
delegation waits for explicit approval forwarding and data-release policy.

### P2: evidence, memory, and UI

#### Carry execution attribution and support deterministic replay

Add an `ExecutionContext` through `contextvars` for turn, workspace, channel,
core, tool/action, Temporal workflow/activity, task, trace, and delivery IDs.
Background tasks must inherit attribution, cancellation, and exception
reporting. Deterministic replay fixtures should exercise routing, permission
decisions, tool contracts, and rendered delivery without network or cost; live
canaries remain independent evidence.

#### Evolve memory conservatively

Define typed memory entries and recall results with provenance, timestamps,
sensitivity, conflict/dedup state, retrieval reason/score, and a token/result
budget. Provide export, purge, and audit. Benchmark BM25 against optional
hybrid embeddings on Pynchy's corpus before adopting vectors, and ask each core
to surface compaction/context-loss events instead of hiding them in the host.

#### Defer schema loading and dashboards until justified

If MCP schemas measurably consume prompt budget, expose compact capability
summaries plus an authorized tool-search/load action while keeping host policy
and grants authoritative. Build a browser dashboard only after authentication
and stable operator schemas exist; it must render those records rather than
introduce a second source of truth.

## Patterns to preserve and ideas to reject

Preserve container isolation, data-flow trust, credential boundaries, Temporal
durability, ActionSpec coverage, live canaries, and worktree ownership.

Do not copy direct provider proliferation, a second agent loop, channel-count
roadmaps, a separate SOP scheduler, a giant channel trait, a full WASM host,
hardware/localization without a concrete use case, ZeroClaw's very large
modules, or feature claims with only partial wiring. Count verified behaviour,
not names.

## Recommended sequence

1. Fail-close and authenticate the control plane.
2. Introduce typed manifests and one resolved-capability view.
3. Generate operator truth and ship read-only doctor/validation/status.
4. Establish the attachment/outbox and external action lifecycle.
5. Add event-triggered Temporal flows and bounded delegation.
6. Add attribution/replay and conservative memory contracts.
7. Consider deferred schemas and a dashboard after the underlying records
   exist.

## Evidence pointers

### Pynchy

- `src/pynchy/plugins/hookspecs.py` and `src/pynchy/plugins/registry.py` —
  plugin registration and raw metadata boundaries.
- `src/pynchy/actions.py` and `docs/architecture/action-coverage.md` —
  semantic action and canary evidence.
- `src/pynchy/host/orchestrator/temporal/` — durable schedules and workflows.
- `src/pynchy/host/container_manager/security/` — trust and taint policy.
- `src/pynchy/state/outbound.py` and
  `src/pynchy/host/orchestrator/messaging/` — delivery state and reconciliation.

### ZeroClaw

- `docs/book/src/ops/network-deployment.md` — two-key remote posture.
- `docs/book/src/architecture/logging.md` — inherited attribution.
- `crates/zeroclaw-eval/src/lib.rs` — replay runner pattern.
- Plugin, channel, SOP, and deployment documents at the recorded revision —
  capability declarations, partial wiring, and operational contracts.
