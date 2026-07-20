# Comparison to OpenClaw

This report compares Pynchy at `a45fa857` with
[OpenClaw](https://github.com/openclaw/openclaw) at
`3659c85e534fdb8b8ce6b7505a83d92cc2e4df8e` (the default branch checked out
for this analysis). It identifies product contracts and design patterns worth
adopting. It is not a feature-count race.

## Conclusion

Pynchy should not become a Python clone of OpenClaw. OpenClaw gets its breadth
from a host-centric Gateway, native-device applications, and a very large
in-process extension surface. That would conflict with Pynchy's best design
decision: containers and per-workspace trust policy are the normal execution
boundary, not an optional safety mode.

Pynchy should adopt four OpenClaw-style contracts:

1. A core-aware runtime capability inventory.
2. A durable external-action lifecycle from draft to confirmed receipt.
3. Pynchy-owned background task, delegation, and handoff semantics.
4. An operator control plane built on those durable records.

The first two make later integrations safer and more reliable. The third
makes parallel work useful across Claude, Codex, and future cores. The fourth
turns Pynchy's strong host machinery into something operators can inspect
without SQLite archaeology.

## Scope and evidence

| Repository | Revision inspected | Evidence |
| --- | --- | --- |
| Pynchy | `a45fa857` | Runtime code, tests, architecture and usage docs, and backlog |
| OpenClaw | `3659c85e` | Source, plugin manifests, architecture, scheduling, task, recovery, and security docs |

OpenClaw has 148 `openclaw.plugin.json` manifests and 11,112 TypeScript source
files in this checkout. This indicates ecosystem breadth and implementation
cost, not that every adapter belongs in Pynchy.

An earlier Pynchy capability-gap note described outbound delivery as
fire-and-forget. Current source supersedes that finding: Pynchy has a
per-channel outbound ledger and reconciliation retries. Focused evidence:

```text
uv run pytest tests/test_reconciler.py tests/test_outbound.py
24 passed
```

## Architecture comparison

| Dimension | Pynchy today | OpenClaw today | Assessment |
| --- | --- | --- | --- |
| Normal execution boundary | Per-workspace Apple Container/Docker agent runs, explicit mounts; host mode requires an admin workspace and explicit `cwd`. | Main session generally runs on the host; non-main sandboxing is configurable. | Pynchy is stronger by default. Do not copy OpenClaw's host-first default. |
| Trust policy | Mount allowlist, workspace isolation, taint-aware service policy, capability allow/deny/approval, secret scanning. | Pairing, gateway auth, tool policies, sandboxing, execution approvals. | Borrow device pairing and signed control-plane identities, but retain Pynchy's data-flow policy. |
| Work durability | Temporal schedules, retries, interrupted-turn, channel reconciliation, deploy, and canary workflows. | Gateway-owned SQLite cron, tasks, and recovery state. | Pynchy has the better substrate. Enrich its records; do not replace Temporal. |
| Agent cores | Claude SDK/CLI, OpenAI, and Codex with generated core configuration and shared policy hooks. | Native runtime plus optional ACP/CLI harnesses and many providers. | Pynchy needs core-neutral contracts for capability truth and delegation. |
| Plugin architecture | Pluggy hooks, static built-ins plus Python entry points; many hooks return raw `dict`s. | Manifest-first discovery, metadata snapshots, typed capability registry, runtime registration, diagnostics. | Adapt OpenClaw's metadata/control-plane split while keeping pluggy. |
| Operator surface | `/health`, `/status`, canary reports, CLI doctor, SQLite, and Phoenix traces. | Gateway protocol, CLI doctor/status, task ledger, Control UI, native apps. | Pynchy has raw evidence, but needs productized operational records and query surfaces. |

## What Pynchy already does better

### Container-first isolation and trust-aware access

Pynchy's ordinary agents run in containers with explicit mounts. Its
`SecurityPolicy` reasons about untrusted input, secrets, public sinks, and
named capabilities. OpenClaw's README describes host execution as the default
for its main session, applying sandboxing selectively. That is a rational
single-user trade-off, but not Pynchy's threat model.

### Temporal is a stronger base for durable work

Pynchy's `InteractiveMessageWorkflow`, `InterruptedTurnWorkflow`, scheduled
agent-task workflows, deploy workflow, canary workflow, and reconciliation
workflow provide durable scheduling, retries, heartbeats, and identity. Borrow
OpenClaw's detailed run/delivery semantics on top of Temporal; do not introduce
a second scheduler.

### Semantic action coverage and canaries

Pynchy has semantic `ACTION_SPECS`, an action-coverage pytest gate, and
real-service canaries with independent verification and cleanup. The action
catalog is more rigorous than a generic adapter smoke test. It should become a
runtime input to capability discovery rather than remain mainly a test and
policy artifact.

### Outbound retries are already the right shape

`sender.py` records per-channel deliveries, `reconciler.py` retries pending
rows in creation order, and `state/outbound.py` persists the delivery state.
Tests cover retry success, retry failure, and ordering. Basic fire-and-forget
delivery is therefore not an open gap.

## High-value gaps and patterns to adopt

### P1: Expand descriptor coverage and add pre-import plugin metadata

**Implemented foundation.** Pynchy now owns immutable `CapabilityDescriptor`
and `HostActionDescriptor` types, validates host-action completeness at
startup, resolves workspace status, and exposes it through `pynchy doctor`,
`/capabilities`, and `/status`. Matrix supplies explicit descriptors; a strict
legacy adapter keeps existing service handlers on the same catalog.

**Remaining gap.** Agent-core, MCP-server, workspace, and channel hooks still
need owned boundary types. Pynchy also cannot inspect a third-party plugin's
identity, compatibility, configuration prerequisites, or optional dependencies
before importing its code.

**Adopt.** Extend the existing descriptor model to those plugin surfaces with
legacy adapters during migration. Add a small `pynchy_plugin.toml` or
`pynchy.plugin.json` only for pre-import package metadata. Runtime registration
remains authoritative, and the manifest must feed the existing capability
inventory rather than create a second registry.

**Proof of completion.** A disabled plugin, missing credential, failed MCP
probe, policy denial, and core-incompatible skill each produce a distinct
inventory state and remediation before an agent attempts the capability.

### P0: Transactional external actions, not approval prompts alone

**OpenClaw pattern.** Restart recovery records source-delivery intent and only
treats confirmed provider success as a receipt. A crash with unknown provider
outcome fails closed instead of replaying a side effect. Its scheduler keeps
execution outcome, delivery outcome, intended/resolved target, diagnostics,
idempotency, and failure-notification states separately.

**Pynchy state.** Pynchy has a file-backed approval state machine, an IPC
idempotency claim for host-mutating requests, and an outbound delivery ledger.
The current Matrix communications gateway shows the narrow policy gate working:
its external send requires human approval. These are useful primitives but do
not form a universal end-user action record: canonical draft payload ->
approval identity -> execution claim -> provider attempt -> receipt or unknown
outcome.

**Adopt.** Create an owned `ActionIntent` / `ExternalAction` state machine:

```text
drafted -> awaiting_approval -> approved -> claimed -> executing
       -> confirmed | failed | denied | expired | outcome_unknown
```

Record canonical payload, read/source references, actor and recipient,
policy decision, approver identity/time, idempotency key, provider request ID,
provider receipt, attempts, and a redacted audit summary. Never automatically
retry `outcome_unknown` unsafe writes. Existing IPC ledger code can implement
the claim; the delivery ledger can become the delivery-specific case.

Start with a drafted mail outbox, not an arbitrary send API. Calendar invites,
browser form submissions, X posts, and future payment actions should consume
the same contract.

**Proof of completion.** Kill Pynchy after a provider accepts a write but
before response persistence. Recovery reports an unknown outcome and requires
reconciliation or human direction; it never sends again silently. Providers
with idempotency support can instead prove the existing receipt.

### P1: Core-neutral delegation and task ledger

**OpenClaw pattern.** `sessions_spawn` gives each subagent an isolated or
explicitly forked session, constrained tools, model/thinking choice, optional
sandbox, task record, cancellation, and push completion to the requester. Its
task ledger tracks `queued`, `running`, `succeeded`, `failed`, `timed_out`,
`cancelled`, and `lost`, with delivery audits and retention.

**Pynchy state.** Claude `Task` sidechains are an implementation facility, not
a Pynchy product contract. Native Teams remain correctly disabled because
shared transcripts can corrupt resumption. Scheduled tasks exist, but Pynchy
has no general interactive child-run record that is inspectable, cancellable,
and recoverable across cores.

**Adopt.** Add host-owned `delegate` and `handoff` backed by a durable
`child_runs`/`task_runs` table and Temporal workflow:

- isolate session/worktree by default; fork only when explicitly requested;
- persist parent, child, input artifact, core/model, allowed capabilities,
  worktree, deadline, cancellation, and cleanup metadata;
- deliver exactly one idempotent handoff to the original requester route/thread;
- treat child output as evidence for parent review, never as instruction text;
- provide status, audit, cancellation, and bounded retention.

Keep Claude `Task` as a backend detail. Do not re-enable native Teams until
each teammate owns an isolated transcript and reports through this contract.

### P1: Productize operations before building a dashboard

**OpenClaw pattern.** Its typed control protocol feeds CLI, Control UI, doctor,
restart recovery, and task notifications from the same task, cron, plugin,
device, approval, and configuration records.

**Pynchy state.** `/status`, canaries, SQLite events, and Phoenix are
strong raw surfaces. They do not yet make a single queryable model of current
capabilities, task runs, approval/action state, delivery debt, MCP readiness,
and channel health.

**Adopt.** First add read-only resources:

- `/capabilities` for effective descriptors and remediation;
- `/tasks` and `/tasks/<id>` for interactive, scheduled, and child-run state;
- `/actions` and `/actions/<id>` for transactional external actions;
- `/delivery` for pending/dead-letter rows and oldest age;
- richer `/status` component state, last reconciliation, and degradation data.

Then add `pynchy doctor` for manifests, config, credential references, runtime
availability, plugin compatibility, and canary evidence. Only after the model
stabilizes should Pynchy choose a web control plane or a native
companion.

### P1: Safe setup and secret onboarding

**OpenClaw pattern.** Plugin manifests surface setup/auth metadata and schema
before executable loading; onboarding and doctor can state what is missing.
Configuration uses validation and concurrency guards.

**Pynchy state.** Google has a specific setup flow, while the backlog already
identifies non-transcripted secret entry. Other integrations still require
manual configuration and credential work.

**Adopt.** Capability-driven setup should run: required config ->
non-transcripted secret entry -> provider authorization -> runtime probe ->
available capability. Never put a pasted secret in conversation state or a
generic agent transcript. Config writes need compare-and-swap revision
protection and a managed restart/reconciliation plan.

### P2: Media and devices, only after a real workflow demands them

**OpenClaw pattern.** Paired nodes advertise camera, screen, location, system
run, voice, and canvas capability. Voice has explicit speech, transcription,
and real-time contracts; native apps are Gateway adapters.

**Pynchy state.** Desktop control/screenshot and inbound Discord audio
transcription already exist. `OutboundEventType` models text and trace events
only; channels lack a general media-send contract. There is no paired-device
capability registry or native companion.

**Adopt in stages.** Define typed `MediaAttachment` plus channel media
capability descriptors, then implement outbound audio/file/image delivery and
TTS for one high-value channel. If cross-device control becomes necessary, add
a narrow paired-node protocol with device-key pairing, signed capability
declaration, per-capability approval, idempotency, and an explicit network
boundary. Do not begin with iOS/Android apps or a generic remote shell.

### P2: Select integrations by workflow, not ecosystem count

OpenClaw supports far more channels and providers. Pynchy already covers
Discord, Slack, WhatsApp, a host-only Matrix communications gateway,
calendar, Google Drive read access, Linear, Proton Mail read operations,
browser/desktop control, X, notebooks, and a LiteLLM provider gateway.

Recommended expansion order:

1. Mail draft/send and calendar invitation/RSVP after `ActionIntent` exists.
2. Drive/document writes and shared documents through the same receipt model.
3. Outbound voice/audio after a media contract exists.
4. Add a channel only for a named workflow. Telegram, iMessage, and Signal may
   be good candidates; Matrix already has a host-only communications gateway.
   Copying OpenClaw's whole channel matrix is not a plan.
5. Implement home automation, health, finance, contacts, Cloudflare, and AWS
   as plugins over the same capability/action contracts, not special host code.

## Specific design lessons

### Keep plugin metadata separate from runtime code

This is OpenClaw's strongest extensibility pattern. Pynchy currently imports a
Python plugin before it can know its config schema or explain unavailability. A
typed manifest permits validation, planning, and diagnosis without running
third-party code. It does not make plugin code safe: in-process Python code
still needs provenance, version pinning, review, and capability disclosure.

### Make durable records drive all operator surfaces

OpenClaw's CLI, UI, doctor, recovery, and notifications use the same records.
Pynchy should not create a second dashboard-specific state layer. Tables and
APIs for capabilities, task runs, action intents, and delivery rows must be
authoritative; HTTP and chat notifications should render them.

### Separate execution outcome from delivery outcome

An agent can complete while the notification fails. A provider can accept a
mutation before the host crashes. These must be distinct states; that removes
false success and unsafe retry cases without clever heuristics.

### Isolate work by ownership, not convention

OpenClaw's isolated/forked sessions are the right starting point. Pynchy should
also give coding children their own worktree and move artifacts/results over a
typed handoff. The current Teams restriction becomes a permanent safety
property rather than a temporary missing feature.

### Use capabilities, not provider branches

OpenClaw distinguishes a plugin ownership boundary from a shared capability.
Pynchy should define `calendar.invitation.send` or `media.voice.send` once,
then let plugins implement it. That fits composition over inheritance and
prevents provider-specific conditionals in core code.

## What not to mirror

- Host-first default execution.
- A monolithic Gateway that owns every provider-specific behavior.
- Adapter count as a roadmap; every adapter adds setup, security, delivery,
  tests, and maintenance work.
- A web dashboard before the durable model exists.
- An unreviewed plugin marketplace.
- Automatic retry of an ambiguous external write.

## Recommended dependency order

1. Define `CapabilityDescriptor` and plugin metadata; expose one inventory to
   host tools, skills, `/status`, and doctor.
2. Define `ActionIntent`; move mail draft/send to it with approval,
   idempotency, receipt, and recovery tests.
3. Add task-run ledger plus Pynchy-owned delegate/handoff on Temporal; prove it
   with a small source-audit/research child run.
4. Productize capability, task, action, and delivery diagnostics through
   read-only APIs and `pynchy doctor`.
5. Add calendar invitation, document-write, and media workflows as consumers.
6. Add channels or paired devices only when a concrete workflow warrants them.

## Evidence pointers

### Pynchy

- Plugin hooks and registry: `src/pynchy/plugins/hookspecs.py`,
  `src/pynchy/plugins/registry.py`.
- Semantic actions/canaries: `src/pynchy/actions.py`,
  `docs/architecture/action-coverage.md`.
- Temporal workflows: `src/pynchy/host/orchestrator/temporal/workflows.py`.
- Outbound ledger/reconciliation:
  `src/pynchy/host/orchestrator/messaging/sender.py`,
  `src/pynchy/host/orchestrator/messaging/reconciler.py`,
  `src/pynchy/state/outbound.py`.
- Security policy: `src/pynchy/host/container_manager/security/middleware.py`.
- Tasks and Teams constraint: `docs/usage/scheduled-tasks.md`,
  `backlog/0-proposed/reintroduce-teams-session-isolation.md`.

### OpenClaw

- Gateway/pairing: `docs/concepts/architecture.md`.
- Plugin metadata/capabilities: `docs/plugins/manifest.md`,
  `docs/plugins/architecture.md`.
- Subagents/handoff: `docs/tools/subagents.md`.
- Scheduler, delivery, task lifecycle: `docs/automation/cron-jobs.md`,
  `docs/automation/tasks.md`, `src/cron/types.ts`, `src/cron/delivery.ts`.
- Recovery receipts: `docs/gateway/restart-recovery.md`.
- Nodes, voice, control plane: `docs/nodes/index.md`, `docs/nodes/talk.md`,
  `docs/web/control-ui.md`.
