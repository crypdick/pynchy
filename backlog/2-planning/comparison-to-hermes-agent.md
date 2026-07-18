# Comparison to Hermes Agent

## Conclusion

Hermes Agent has a substantially broader product surface: a terminal-first
agent, a large channel gateway, direct provider and credential management,
session navigation, profiles, a dashboard, and extensive built-in tools. Its
best ideas for Pynchy are not the breadth itself. They are the small explicit
contracts that let a broad system remain operable:

- A capability descriptor that unifies registration, readiness, setup hints,
  and the user-facing explanation for why something is unavailable.
- A channel descriptor that states what a transport can really do instead of
  discovering optional methods at call sites.
- First-class, durable lifecycles for a delegation result and for a scheduled
  task dependency.
- Deliberate session lineage, search, export, and handoff semantics.
- Correlation IDs that join agent, tool, approval, delivery, and scheduling
  evidence without duplicating sensitive trace bodies.

Pynchy should adopt those patterns in ways that strengthen its own container,
workspace, Temporal, LiteLLM, and security-policy boundaries. It should not
copy Hermes's default local terminal execution, direct credential pools,
single-process cron scheduler, thread-pool delegation, import-time runtime
discovery, or unconstrained self-modifying skills. Those choices suit a
terminal-first personal agent; they would weaken Pynchy's strongest properties.

The first two implementation priorities are a typed resolved capability
snapshot and a host-owned attachment/outbound-message contract. They unblock
operator diagnostics, rich interactions, files, voice, and better channel
plugins without committing Pynchy to a giant gateway rewrite.

Pynchy main advanced while this research ran with approved backlog entries for
Sherpa ONNX TTS and a managed flow model on Temporal, then converted Matrix
communications from a remote script MCP into native IPC-backed agent tools and
added one bounded Discord voice workspace. Those changes confirm rather than
invalidate this order: the media contract generalizes the new voice special
case safely, managed flow supplies the product state model for durable
handoffs, and Matrix becomes the best first pilot for a resolved capability
descriptor.

## Scope and evidence

This analysis compares source trees rather than marketing claims.

| Subject | Snapshot inspected | Evidence |
| --- | --- | --- |
| Pynchy code at analysis start | 93a3b692b9b7bf24bc0310addc3347ca9075d447 | Managed report worktree created from Pynchy main |
| Pynchy control main at completion audit | b128527702376a280ac9463763a3c7ed4bbf3de1 | Seven later commits audited: approved TTS/managed-flow planning, Matrix native IPC tools with action mappings/tests/docs, Proton planning accuracy, and a bounded Discord voice workspace with host STT/local TTS |
| Hermes Agent | bf517f930144704c586be6f9deca30b072586c76 | Shallow clone of NousResearch/hermes-agent main |

The Pynchy snapshot contains 365 Python source files, 182 Python test files,
and 77 semantic action specifications. Hermes contains 2,106 Python test
files, 94 tool-registration call sites, and 81 Python files directly under
its tools directory. These counts establish scope only. They do not measure
quality, security, or feature completeness.

The comparison uses the implementation and its accompanying documentation as
evidence. In particular:

- Hermes documents one AIAgent behind CLI, gateway, ACP, batch, and API
  entrypoints, with SQLite plus FTS5 sessions, 70+ tools, 28 toolsets, six
  terminal backends, and a broad channel gateway.
- Pynchy documents a host orchestrator that runs agent cores in containers,
  plugin-provided channels and services, a LiteLLM gateway, SQLite state, and
  Temporal workflows for recovery and scheduled work.
- Pynchy source confirms the important boundaries: semantic action coverage,
  per-workspace MCP virtual keys, the outbound delivery ledger, durable
  in-flight turns, and the four-property service trust policy.

The final Pynchy delta moves Matrix list and send operations through the
built-in agent tool server and host-side IPC service handlers. Each operation
has an ActionSpec entry, explicit trusted-profile selection, and normal
approval gating for the external send. It removes a separately hosted script
MCP server. This is a positive simplification and an ideal descriptor pilot,
but it does not yet provide a general availability, setup, health, or doctor
contract.

The final delta also adds one explicitly configured Discord voice workspace.
It records a voice JID, applies the normal workspace/member policy, transcribes
bounded spoken turns through the host audio service, and sends only final agent
text through a configured local TTS command. It is intentionally a narrow
Discord-specific implementation, not a generic attachment, media-event, or
multi-channel delivery contract.

This report distinguishes three things throughout:

1. A current capability, demonstrated in the inspected tree.
2. A pattern worth adapting, with a Pynchy-specific shape.
3. A capability deliberately deferred or rejected because it conflicts with
   Pynchy's design.

## Architecture at a glance

| Concern | Hermes Agent | Pynchy | Assessment |
| --- | --- | --- | --- |
| Primary runtime | One large general-purpose AIAgent with a tool registry | Pluggable agent cores launched by a host orchestrator | Pynchy's separation makes core replacement and host policy cleaner. Hermes has a more cohesive end-user runtime. |
| Execution boundary | Local terminal by default; Docker, SSH, Modal, Daytona, Singularity, and other backends optional | Container by default; direct host execution exists only as an explicit trusted workspace mode | Pynchy has the safer default. Do not change it to gain Hermes parity. |
| Provider routing | Direct provider resolver, credential pools, several API modes | LiteLLM gateway with virtual keys and agent cores that do not receive provider secrets | Pynchy should retain the gateway boundary. Hermes's provider UX can inform diagnostics, not a parallel provider layer. |
| Tools | Self-registering handlers grouped into toolsets, each with availability checks | MCP tools, IPC service handlers, skills, agent-core tools, and semantic action specifications | Hermes has a better availability presentation. Pynchy has better effect-level evidence. Combine the two. |
| Channels | One gateway with many platform adapters and a feature matrix for media, threads, reactions, typing, and streaming | Channel plugins with a small protocol; Discord, Slack, TUI, and WhatsApp packages in the source tree. Discord now supports audio-attachment transcription and one configured voice workspace with host STT/local-TTS final replies. | Hermes is broader. Pynchy needs a stronger generic channel contract before extending its bounded Discord voice path or adding breadth. |
| Sessions | SQLite plus FTS5, lineage, titles, cross-surface handoff, search, and export | Group-isolated core sessions, transcript archives on compaction, structured memory recall, and restart recovery | Hermes offers better user navigation. Pynchy offers stronger execution recovery and workspace isolation. |
| Background work | Cron JSON, one-minute polling, process-local task execution; background delegation preserves completed results but cannot resume a running child | Temporal schedules and workflows, heartbeat-based activities, in-flight turn recovery, ordered delivery retries | Pynchy is materially stronger for durable work. Adopt Hermes's job composition UX on top of Temporal. |
| Security | Approval modes, deny lists, user allowlists, optional sandboxes and network controls | Container/mount boundary, service trust policy, taint tracking, secrets scanning, approval state, audit, and capability policy | Pynchy has the more coherent security model. Hermes patterns must compose with it, never bypass it. |
| Extensibility | Plugins can register tools, hooks, commands, memory providers, context engines, and platforms | Pluggy hooks provide agent cores, channels, service handlers, skills, memory, MCP servers, workspaces, observers, runtimes, and tunnels | Both are extensible. Hermes descriptors are more operationally expressive; Pynchy hooks need typed operational metadata. |
| Observability | Versioned observer payloads with session, turn, request, tool, approval, and child identifiers | SQLite operational events, Phoenix LLM traces, Temporal status, action coverage, and canaries | Pynchy has better validation and less duplicate trace storage. It needs a shared correlation contract. |

## What Pynchy already does better

### Security follows trust and data flow, not only command text

Pynchy's SecurityPolicy reasons about public source, secret data, public sink,
and dangerous writes. It carries corruption and secret taint across host-side
service operations, gates lethal-trifecta combinations, scans outbound payloads
for secrets, and records security events. The container Bash gate closes a
separate exfiltration path.

Hermes has useful approval modes, denial rules, protected paths, and optional
sandbox backends. Its default terminal backend remains local, however, and its
approval system primarily classifies commands. Pynchy should not trade
data-flow-aware policy for a larger command allowlist.

### Durable execution and recovery are first-class

Pynchy writes an in-flight turn checkpoint before invoking the agent and uses
Temporal to run interactive turns, interrupted-turn recovery, scheduled tasks,
deploy handoffs, learning reviews, channel reconciliation, and canaries. It
resumes semantically after process loss and keeps idle sessions distinct from
active work.

Hermes handles completed background delegation delivery carefully, including a
durable claim. Its own documentation explicitly records a running child as
unknown after process exit because it cannot prove the side effects. That is
the correct outcome for its architecture, but Pynchy already has a better
durability substrate for general handoffs.

### Pynchy proves effects rather than only exposing tools

Pynchy's 77 ActionSpec entries map user-meaningful effects to hermetic tests.
Provider-facing actions can require a real-service canary with an independent
verifier and cleanup. The canary report keeps establishment failures distinct
from regressions.

Hermes's tool registry has an elegant availability check, but a registered
tool does not by itself prove the remote effect. Pynchy should preserve the
action catalog as the canonical semantic contract when adding any registry
descriptor.

### MCP access has a useful multi-tenant boundary

Pynchy starts MCP instances on demand, deduplicates compatible instances, and
gives each workspace a LiteLLM virtual key scoped to allowed MCP servers.
Containers do not receive provider secrets. Hermes can dynamically expose MCP
tools, but Pynchy's team-key and lifecycle model gives a cleaner basis for
separating workspaces and credentials.

### Workspace and queue semantics are explicit

Pynchy groups combine a workspace, chat routing, profile, trust policy,
session location, and per-group queue. A shared group can intentionally
broadcast across mapped channels while work remains serialized under global
concurrency limits. Hermes profiles isolate whole HERMES_HOME directories,
which solves a different problem and is not a substitute for Pynchy's
workspace-level trust boundary.

### Learning already has more governance than it first appears

Pynchy's learning review runs through Temporal, sends a bounded turn packet to
a hidden reviewer, writes to the Obsidian namespace, and makes learned skill
selection profile-controlled. Agents discover and request skill access rather
than silently receiving every learned skill. Hermes's skill loop is a valuable
product capability, but Pynchy does not have a fundamental learning gap.

The current broad read-write vault mount remains a security debt. More
aggressive automatic skill mutation would make that debt worse, not better.

## Capability gaps worth adapting

### Implemented foundation: typed host-action capability snapshots

Pynchy now owns immutable capability and host-action descriptors, validates
their ActionSpec, approval, idempotency, and audit contracts at startup, and
resolves workspace-specific status for operator diagnostics. The first Matrix
slice exposes those results through `pynchy doctor`, `/capabilities`, and
`/status` while keeping `SecurityPolicy` authoritative at dispatch time. See
[Action coverage](../../docs/architecture/action-coverage.md#host-action-descriptors-and-capability-status).

The remaining cross-plugin descriptor and pre-import metadata work belongs to
the [OpenClaw comparison](comparison-to-openclaw.md#p1-expand-descriptor-coverage-and-add-pre-import-plugin-metadata).

### P0: Host-owned media, attachment, and channel-capability contracts

#### The gap

Hermes's messaging documentation makes transport differences visible. It
states per-platform support for voice, images, files, threads, reactions,
typing, and streaming. Its adapters receive and send media, while TTS and
transcription use structured cache paths behind the delivery renderer.

Pynchy has an OutboundEvent with text content and untyped metadata. The channel
protocol has a single send_event method and optional methods discovered with
hasattr. Approval widgets add one rich approval event, but the model has no
generic attachment envelope or capability descriptor. The result blocks
outbound files, portable media attachments, generic interactive forms, and a
clean way to degrade when a channel cannot support a feature.

The completion-audit Discord voice workspace proves that host STT and local
TTS can work safely for one voice JID. It intentionally turns a final text
event into live Discord voice playback, rather than adding a portable audio
attachment to OutboundEvent. That is a sensible narrow product slice, but it
cannot yet deliver a generated file, a voice note, or the same audio artifact
through a normal Discord text channel, Slack, WhatsApp, or another plugin.

#### Pynchy-native design

Keep host ownership of every file and add narrow semantic types:

| Type | Responsibility |
| --- | --- |
| InboundAttachment | Source channel, opaque remote ID, MIME type, size, safe cache reference, hash, and optional transcript |
| OutboundAttachment | Staged host-owned content reference, MIME type, filename, disposition, and checksum |
| OutboundMessage | Text, attachments, reply/thread target, interaction request, and delivery intent |
| ChannelCapabilities | Attachments, voice, native thread, message update, reaction, typing, approval action, generic form, and size constraints |
| DeliveryAttempt and DeliveryResult | Ledger ID, channel ID, remote message ID, accepted or failed state, retry classification, and safe error detail |

Inbound channel adapters download only into a host-owned staging area after
size and MIME checks. The host scanner validates containment, hashes content,
and stores metadata on NewMessage. The agent receives a safe text
representation and opaque attachment identifiers, never a trusted arbitrary
host path.

Outbound adapters consume a staged attachment only after the host validates
its hash, content type, size, target policy, and channel capability. The
outbound ledger should distinguish at least:

1. Persisted for delivery.
2. Accepted by the channel API, with a remote message identifier if supplied.
3. Reconciled or receipt-confirmed when the transport can prove it.
4. Permanently failed or outcome unknown.

The existing Pynchy ledger and ordered retry behavior are a strong base. Do
not adopt Hermes's text marker convention as the public protocol; a raw
media-path tag would reintroduce filesystem trust into agent output.

#### First vertical slice

Use a Discord text channel separate from the bounded voice workspace:

1. Receive an image or general file into InboundAttachment.
2. Send a staged text-plus-file response through OutboundMessage.
3. Persist remote message ID and acceptance state.
4. Render a deterministic text fallback when a second channel lacks
   attachments.
5. Test stage cleanup, hash mismatch rejection, path containment, retry
   ordering, no-cross-workspace attachment access, and fallback rendering.

Before extending the current Discord voice special case to another channel,
shipping generic voice notes, or adding generic question widgets or reactions,
complete this slice. It preserves the earlier voice/TTS analysis: shared
transcription is straightforward host preprocessing, while portable TTS needs
a reliable media delivery contract first.

The approved Sherpa ONNX TTS plan and the new local-command voice workspace
need the same future boundary. Treat OutboundAttachment,
ChannelCapabilities, and DeliveryResult as their shared upstream contract
rather than adding another plugin-specific media event or cache lifecycle.

### P1: Separate transcript navigation from durable memory

#### The gap

Hermes exposes scoped FTS5 session search, session titles, lineage-aware
resume, redacted export formats, and CLI-to-channel handoff. This makes a long
history inspectable and portable without asking the agent to remember
everything.

Pynchy already offers group-isolated structured memory, BM25 recall, and a
transcript archive on compaction. Those are valuable, but they solve different
jobs. Memory stores reusable facts; a compaction archive preserves a point in
a conversation. Neither provides an operator or agent a first-class,
permission-scoped history query and export surface.

#### Pynchy-native design

Add a ConversationIndex owned by the host, not the agent core. It indexes
archive records and message metadata under a workspace scope. The index should
carry:

- Workspace and canonical chat identifiers.
- Core-neutral session lineage and compaction predecessor/successor IDs.
- Display title, source channel, timestamp range, and redaction state.
- Searchable transcript body only when that workspace permits it.
- Export jobs that require a redaction policy and explicit destination.

Expose scoped searches such as current workspace, named workspace for an
admin, and explicitly selected set of workspaces. Never make global transcript
search the default for an agent in a shared or non-admin group.

Keep structured memories separate. A recall result means a durable fact was
selected for future use. A conversation-search result means an archive
fragment exists. Conflating them would cause accidental persistence and
cross-session leakage.

#### Handoff sequencing

Pynchy already broadcasts a workspace across mapped channels. A future
explicit handoff should mean moving a particular session to a capability-aware
thread or target channel, not silently sharing a transcript with every
participant who reaches a group. Build it only after ChannelCapabilities and
ConversationIndex exist. The operation must record the source, target,
visibility, session lineage, and fallback behavior for transports without
threads.

### P1: A generic durable handoff, not native multi-agent transcript sharing

#### The gap

Hermes's delegate_task has several elegant safeguards: children inherit, but
cannot widen, the parent's toolsets; leaf children cannot use memory or user
clarification; background completions arrive as a fresh turn rather than being
spliced into the live transcript; completion delivery has a durable claim.

Pynchy permits safe Task sidechains but keeps native Teams disabled because
sharing a leader transcript could cross an isolation boundary. That is the
right choice. There is no core-neutral host contract for a background
reasoning job whose result can be retrieved, cancelled, audited, and recovered
without coupling it to an agent SDK's private session model.

The approved managed-flow model supplies the right product state layer for
this work: owner/requester identity, typed wait and cancellation states,
revisioned transitions, parent/child relationships, and operator-facing flow
status. Handoff should become a managed-flow kind backed by the contract
below, not a separate ad hoc subagent table.

#### Pynchy-native design

Create a Temporal-backed Handoff record:

| Field | Reason |
| --- | --- |
| Handoff ID, parent turn ID, and workspace | Correlation and policy scope |
| Source core and worker backend | Preserve core neutrality |
| Immutable capability snapshot | Child cannot gain capabilities after launch |
| Bounded input artifact references | Avoid copying a live transcript or mounting more data |
| Result schema and artifact allowlist | Keep parent context small and result handling safe |
| State and idempotency key | Queued, running, completed, cancelled, failed, unknown |
| Approval and cancellation state | Operator control with durable audit |
| Delivery receipt state | Result becomes a new turn only after durable acceptance |

Claude Task, Codex delegation, a future remote worker, and an evaluation
runner can all implement the backend interface. None should decide the
security policy or persist a child result directly into the parent transcript.

For a successful background result, Temporal starts a fresh interactive
workflow with a structured handoff event. That copies Hermes's best transcript
integrity pattern while using Pynchy's recovery model. A crash before the
external effect becomes a recorded unknown outcome; it must not be retried
blindly.

### P1: Composable scheduled work on Temporal

#### The gap

Hermes cron supports task-local skills, project working directories, delivery
targets, no-agent preflight scripts, and context_from dependencies. These make
automation expressive, even though Hermes executes on a one-minute
single-process scheduler with JSON job storage.

Pynchy ScheduledTask has a strong durable executor but a smaller task shape:
prompt, schedule, group or isolated context, repository access, and status.
Pynchy already separates host jobs from agent tasks, which is important, but
it lacks typed task composition and a safe skip gate.

#### Pynchy-native design

Extend scheduled work through a typed SchedulePlan:

- Preflight capability ID. A registered, bounded host action returns skip,
  run, or run_with_context. It is not arbitrary agent-authored shell.
- Input dependencies. A task consumes only a typed digest or named artifact
  from predecessor tasks, not their whole conversations.
- Execution profile and resolved capability snapshot. The schedule cannot
  widen its current workspace policy.
- Explicit delivery intent. A response can deliver, remain internal, or send
  a bounded host notice; the channel adapter decides the concrete rendering.
- Idempotency and freshness rules. The workflow records which dependency
  version it consumed and whether a run can safely retry.

Start with a read-only change detector that skips a report when nothing
changed. Then add one typed context dependency. Preserve Temporal schedules,
activity heartbeats, retries, durable logs, and canary behavior. Do not replace
them with a host sweep loop or a JSON file scheduler.

### P1: Capability-driven doctor and setup readiness

Pynchy already has a broad HTTP status payload, Temporal status, LiteLLM
readiness, a channel snapshot, and canary endpoints. Hermes adds a practical
doctor and setup experience that tells a user why a provider, terminal, or
platform cannot run.

Once WorkspaceCapabilitySnapshot exists, add a read-only Pynchy doctor:

- Compare desired configuration with installed plugin descriptors.
- Check channel, MCP, LiteLLM, Temporal, and workspace-policy readiness.
- Surface missing optional dependencies and stale canary evidence.
- Show safe recovery commands or configuration paths without inspecting or
  printing secret values.
- Emit human-readable text and stable JSON for TUI, CI, and support tools.

Doctor must not silently modify configuration, refresh credentials, start
untrusted plugins, or restart the service. Status says what runs now; doctor
explains why the desired system cannot safely run.

### P1: Correlated observer contract across existing truth sources

Hermes's versioned observer contract exposes opaque session, turn, request,
tool, approval, and child identifiers while keeping payload sanitation
centralized. This is an elegant interoperability boundary for telemetry.

Pynchy deliberately splits state between SQLite operational events, Phoenix
LLM traces, Temporal workflows, the outgoing ledger, and security audit. That
split is sensible, but current consumers have to infer joins from chat JIDs and
timestamps.

Add a small immutable ExecutionContext carried through host operations:

- Workspace ID and canonical chat ID.
- Session ID and Pynchy turn ID.
- Temporal workflow and activity IDs when applicable.
- Semantic action ID, MCP invocation ID, and approval ID where applicable.
- Outbound ledger ID and remote delivery ID when applicable.

Define a versioned observer event envelope with these IDs and a short,
redacted payload. Leave full model prompts, tool arguments, and provider
responses in Phoenix under its existing privacy policy. This provides
correlation without creating a second trace database.

### P2: Govern learning changes as proposals and evaluations

Hermes treats skill creation and improvement as a visible product loop.
Pynchy already has a hidden learning reviewer and a shared learned-skill
registry, so simple automatic skill creation is not a missing feature.

The useful next step is governance:

1. Emit a LearningChangeProposal with source turn, target path, diff, profile
   ACL effect, and evaluator result.
2. Automatically apply only low-risk memory additions that satisfy a narrow
   policy.
3. Require review for a shared skill change, changed tool instructions, or a
   cross-profile visibility expansion.
4. Run a small replay or static check before a skill becomes eligible for a
   profile.
5. Record the decision in the existing vault and action/canary evidence where
   it changes a real external capability.

This makes Pynchy's existing learner more trustworthy. It does not create a
second skill hub or invite agents to mutate host-side code and configuration.

### P2: Lazy plugin metadata for channel breadth

Hermes's PlatformRegistry is particularly good at having a platform own its
configuration validation, setup instructions, connect check, privacy flags,
message limit, and standalone delivery function. Its deferred loaders also
avoid importing every heavy SDK for a plain CLI session.

Pynchy should adapt this only when it has enough optional channels for startup
cost and operator discovery to become material. The Pynchy version should:

- Keep entry-point discovery and an explicit installed-plugin allowlist.
- Load a declarative descriptor without importing an optional SDK where
  possible.
- Import executable plugin code only after selection and dependency checks.
- Subject host-side plugin code to the existing verification and trust model.

Do not allow a plugin descriptor to redefine global policy, inject arbitrary
startup actions, or silently replace a built-in channel.

## Elegant Hermes patterns to reuse

### Stable prompt boundaries

Hermes treats an active context as immutable: background completions surface
through a fresh turn, and changing toolsets applies only on a reset. This
preserves message-role alternation and prompt caching.

Pynchy already has a per-group queue and a safe tool-result interrupt
boundary. Make the invariant explicit in any handoff and rich-interaction
work: never splice a late result into an active core transcript. Queue or
start a distinct durable turn instead.

### Availability checks as user experience, not authorization

Hermes caches availability checks and remembers a recent success so a transient
Docker probe does not make tools disappear mid-session. This is a good
operability pattern for ready or degraded display.

Use it in Pynchy doctor and capability snapshots, with an important limit:
availability caching cannot approve a service call. SecurityPolicy and the
actual backend must re-evaluate immediately before an external effect.

### A transport contract that includes delivery outside the happy path

Hermes adapter metadata covers connect state, configuration validation,
standalone sends for cron, limits, and privacy flags. Pynchy's existing
outbound ledger adds stronger ordered retries. Combining them produces a better
contract than either system alone: a typed adapter declaration plus durable
accepted, reconciled, and unknown delivery outcomes.

### Minimal child context and capability monotonicity

Hermes children inherit parent toolsets and remove capabilities that would
allow uncontrolled recursion, user interruption, or shared-memory mutation.
Pynchy should preserve the same monotonicity in Handoff: a worker gets an
immutable subset of its parent's resolved snapshot, never an open-ended copy
of the parent environment.

### Explicit session lineage

Hermes gives users names, lineage after compression, resume semantics, and
redacted exports. Pynchy should keep core-specific session pointers private
but maintain a host-level lineage record so history remains understandable
across cores, compactions, deploy recovery, and future handoffs.

## Capabilities to defer or reject

| Hermes capability or pattern | Decision for Pynchy | Reason |
| --- | --- | --- |
| Default local terminal backend | Reject | Pynchy's primary isolation boundary is the container and explicit mount policy. |
| Direct provider clients and credential pools inside the agent runtime | Reject | LiteLLM virtual keys keep real provider credentials out of agent containers. |
| Whole HERMES_HOME clone per profile | Do not mirror | Pynchy groups and workspace profiles already model policy-scoped isolated work; whole-home copies would duplicate state and blur ownership. |
| Large channel-port campaign | Defer | Add ChannelCapabilities and media lifecycle first; capability quality matters more than adapter count. |
| Import-time tool source scanning | Reject | Explicit Pynchy plugin discovery is more reviewable and safer for host code. |
| JSON cron plus one-minute host sweep | Reject | Pynchy already has Temporal schedules, heartbeats, recovery, and durable status. |
| Thread-pool subagents as the durable work substrate | Reject | Use Temporal handoffs so recovery, policy, and audit do not depend on one process. |
| Automatic live-skill rewriting during ordinary conversations | Reject in current form | Pynchy's vault has broad access today; add proposals and evaluation first. |
| Full dashboard, desktop app, and ACP parity | Defer | Useful product work, but it does not unblock secure messaging, delivery, or durable workflows. |
| Provider-specific setup wizard parity | Defer | Improve Pynchy doctor around LiteLLM first; a second provider-management plane would duplicate the gateway. |

## Recommended dependency order

| Order | Deliverable | Depends on | Success evidence |
| --- | --- | --- | --- |
| 1 | CapabilityDescriptor and WorkspaceCapabilitySnapshot for one MCP capability | Existing ActionSpec, SecurityPolicy, MCP manager | Doctor explains every ready and unavailable state; dispatch still rechecks policy; action coverage passes |
| 2 | ExecutionContext event envelope | Turn and workflow IDs | A single request can join SQLite, Phoenix, Temporal, approval, and outbound-ledger evidence without a timestamp heuristic |
| 3 | ChannelCapabilities plus text-and-file OutboundMessage vertical slice | Outbound ledger and one native channel | Attachment send/retry/fallback path, remote ID persistence, containment tests, semantic action entry |
| 4 | ConversationIndex and redacted export | Archive records and ExecutionContext | Workspace-scoped search, lineage, and export tests; no cross-workspace default lookup |
| 5 | Temporal Handoff backend contract | Capability snapshot, ExecutionContext, ConversationIndex | Child cannot widen capabilities; crash outcome becomes explicit; completed result arrives as a fresh durable turn |
| 6 | SchedulePlan preflight and typed dependency | Temporal Handoff/artifact contract | A skipped task, a context-fed task, retry behavior, and delivery policy all have durable evidence |
| 7 | Read-only doctor CLI and setup guidance | Capability snapshot and canary status | JSON and human output identify actionable, safe recovery steps |
| 8 | LearningChangeProposal policy | Capability snapshot and evaluator hooks | Shared-skill changes are reviewable and ACL effects are explicit |
| 9 | Additional channels, voice, generic forms, dashboard, ACP | Channel contract and doctor | Each addition declares channel capability, action evidence, delivery behavior, and operator readiness |

## Evidence pointers

### Hermes Agent

- README.md describes the product surfaces, supported execution backends, and
  terminal versus gateway entrypoints.
- website/docs/developer-guide/architecture.md maps AIAgent, registry,
  session storage, plugin system, cron, and ACP.
- tools/registry.py defines self-registration, capability checks, health
  caching, generation tracking, and plugin override controls.
- gateway/platform_registry.py defines declarative platform metadata,
  validation, readiness, privacy, limits, setup, and lazy loading.
- website/docs/user-guide/messaging/index.md provides the concrete
  transport-feature matrix for voice, attachments, threads, reactions,
  typing, and streaming.
- website/docs/user-guide/sessions.md documents lineage, scoped sessions,
  handoff, search, and redacted exports.
- tools/async_delegation.py and website/docs/user-guide/features/delegation.md
  document durable result delivery and the explicit unknown outcome after a
  process exits during execution.
- website/docs/user-guide/features/cron.md documents skills, preflight,
  no-agent tasks, dependencies, delivery, and its JSON-backed scheduler.
- website/docs/user-guide/security.md documents command approvals,
  allowlists, terminal backend tradeoffs, and optional container isolation.
- docs/observability/README.md defines the versioned observer/correlation
  contract.

### Pynchy

- src/pynchy/types.py defines NewMessage, InFlightTurn, ScheduledTask,
  OutboundEvent, and the Channel protocol.
- docs/architecture/security.md and
  src/pynchy/host/container_manager/security/middleware.py define the
  container, mount, taint, service-trust, capability, and approval boundaries.
- docs/architecture/message-routing.md and
  src/pynchy/host/orchestrator/temporal/scheduler.py document durable
  interactive and scheduled execution recovery.
- docs/architecture/action-coverage.md and src/pynchy/_action_specs.py
  define effect-level test and canary evidence.
- docs/architecture/mcp-management.md defines on-demand MCP lifecycle and
  workspace-scoped LiteLLM access.
- docs/usage/memory.md and docs/architecture/memory-and-sessions.md define
  workspace memory, compaction archives, and the current learning reviewer.
- docs/usage/channels.md, src/pynchy/host/audio.py, and the Discord voice
  modules show the completion-audit bounded voice workspace and explain why
  it is not yet generic outbound media; the current optional davey import
  failure provides the concrete availability-boundary regression.
- docs/plugins/index.md and src/pynchy/plugins/hookspecs.py show the plugin
  categories and loose-dict extension seams.
- docs/usage/matrix-gateway.md and
  src/pynchy/plugins/integrations/matrix_gateway.py show the completion-audit
  native IPC migration and its explicit profile/approval boundary.
- src/pynchy/state/outbound.py and the messaging sender/reconciler own the
  current ordered outbound ledger and retry path.
- docs/architecture/observers.md shows the current intentionally small event
  bus and Phoenix split.
