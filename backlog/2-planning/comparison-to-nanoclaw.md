# Comparison to NanoClaw

## Conclusion

NanoClaw and Pynchy share the important premise: an agent should run inside an
explicit container boundary, not receive ambient host access. They diverge in
product strategy. NanoClaw keeps a small Node host and asks each user to fork
and modify it through skills. Pynchy keeps a larger, typed, plugin-oriented
platform with reusable profiles, host-side policies, multiple agent cores, and
Temporal-backed durable work.

Pynchy should **not** adopt NanoClaw's "copy code into every fork" model, its
single-process sweep scheduler, or its willingness to let an agent mutate its
own runtime configuration after an approval. Those choices make sense for
NanoClaw's bespoke-fork goal, but regress Pynchy's extension, deployment, and
trust boundaries.

The strongest ideas to adapt are narrower:

1. Make host-executed capabilities and approval paths explicit, typed, and
   completeness-tested.
2. Add an end-to-end attachment/outbox contract before adding TTS, voice
   replies, or rich interactive channel features.
3. Promote session topology and core-neutral handoff to deliberate contracts,
   rather than accumulating channel-specific exceptions.
4. Add a deterministic, inspectable setup-plan runner for high-friction
   integrations while retaining Pynchy's declarative configuration.
5. Improve operator recovery with a read-only doctor and a deployment-state
   compatibility check that fits Pynchy's managed auto-deploy path.

This assessment does not recommend a feature-count race. Pynchy already has a
better foundation for secure, durable, testable personal automation; the work
below focuses on the few NanoClaw patterns that would make that foundation more
coherent.

## Scope and evidence

This report compares current Pynchy `main` at `74516faf` (2026-07-17) with a
shallow source checkout of `nanocoai/nanoclaw` at
`082f5c7ea99342fcb324ab78baacb0c4e6894029` (2026-07-17). The NanoClaw checkout
contains 34,499 host `src/**/*.ts` lines and 7,195 agent-runner TypeScript
lines; Pynchy contains 62,046 `src/pynchy/**/*.py` lines. These counts describe
the examined trees only, not dependency quality, extension branches, or product
value.

The report worktree began at `a45fa857`. Before completion, current `main`
advanced to `74516faf`; its four-commit delta adds a Google Workspace planning
item and streamable-MCP proxy fixes, neither of which changes the architecture
claims below. The final source audit used that current control checkout.

NanoClaw's own architecture document labels itself a design draft and directs
readers to source when it drifts. Findings here therefore use the runtime
implementation as the authority, especially `router.ts`, `session-manager.ts`,
`delivery.ts`, `host-sweep.ts`, `container-runner.ts`, and the
`container/agent-runner` code.

The comparison deliberately distinguishes:

- **present** — Pynchy already supports the outcome;
- **adapt** — NanoClaw has a pattern worth reshaping for Pynchy;
- **gap** — a user-meaningful capability Pynchy does not yet offer; and
- **avoid** — an apparent NanoClaw advantage that conflicts with Pynchy's
  architecture or safety model.

## Architecture at a glance

| Concern | Pynchy today | NanoClaw today | Assessment |
| --- | --- | --- | --- |
| Agent isolation | Ephemeral Docker or Apple Container invocation with explicit mounts; direct host mode remains an explicit trusted exception. | Per-session Docker container with an agent-group filesystem and session DB mounts. | **Pynchy ahead.** Preserve the pluggable runtime and explicit host-mode boundary. |
| Host/container protocol | File IPC plus a host-owned SQLite state database, idempotent IPC request claiming, and a per-group queue. | Two SQLite databases per session: host writes `inbound.db`, container writes `outbound.db`; each file has one writer. | **Adapt the ownership discipline, not the transport.** |
| Conversation model | Canonical `chat_jid` conversations broadcast across connected channels; Discord threads create isolated workspaces inheriting their parent profile. | User → messaging group → agent group → session routing with `shared`, `per-thread`, or `agent-shared` session mode. | **Partial gap.** Pynchy supports threads but not a general session-topology contract. |
| Scheduling | Temporal schedules/workflows, retry policies, durable interrupted-turn recovery, config reconciliation, and scheduled canaries. | A 60-second host sweep over session SQLite rows; recurrence and retries live in the session DBs. | **Pynchy ahead.** Do not replace Temporal with polling. |
| Channels | Pluggy channel providers; first-party WhatsApp, Slack, Discord; host-only Matrix communications gateway; canonical multi-channel sync. | Registry/adapter seam plus optional adapter skills on a separate branch; broad advertised channel catalog. | **Mixed.** NanoClaw has breadth; Pynchy has a more reusable extension model. |
| Outbound media | `OutboundEvent` carries text plus formatter metadata. Inbound Discord audio transcription exists, but outbound voice/media and question widgets remain unsupported. | `send_file` writes filename-only records to a per-message outbox; host uploads files; cards, edits, reactions, and question flows share the message protocol. | **Concrete Pynchy gap.** |
| Agent/provider abstraction | Plugin-discovered Claude, OpenAI, and Codex cores; LiteLLM routes model choice; profiles select model and tools. | Provider interface separates runner responsibilities from SDK mechanics; trunk ships Claude, skills install others. | **Pynchy ahead on deployable extensibility; adapt the narrow core/runner boundary if a core needs it.** |
| External action control | `SecurityPolicy`, taint tracking, approval files, IPC idempotency, semantic `ACTION_SPECS`, and service trust declarations. | Nominal guarded-action catalog, central guard decision pipeline, bound approval replay, destination ACLs. | **Pynchy is stronger in policy; NanoClaw's catalog/conformance pattern is worth adopting.** |
| Operations and upgrade | `/health`, `/status`, queue snapshot, SQLite event observer, Temporal status, action coverage, and canaries. | `ncl` operations CLI, session state inspection, crash-start circuit breaker, setup/upgrade flows, and a startup upgrade marker. | **Adapt selected recovery ergonomics.** |
| Customization | TOML config, workspace/profile composition, plugins, MCP specs, and vault-selected skills. | Fork modification and skill-driven code copying; self-modification can alter packages/MCP config after approval. | **Avoid the fork-first runtime model.** |

## What Pynchy already does better

### Durable schedule execution and interrupted-turn recovery

NanoClaw's sweep offers a sensible lightweight solution: it reads due rows,
wakes containers, detects stale processing claims through heartbeats, and
reschedules retries. Its recurrence calculation advances from the intended
scheduled time, which avoids clock-drift. That is good local design, but it
does not match Pynchy's durability. Pynchy creates Temporal schedules and
workflows, gives activities explicit retry policies, and resumes an interrupted
agent turn from a durable checkpoint rather than simply trying the work again.

Keep Temporal as the scheduling authority. A NanoClaw-style task preflight can
be valuable, but it should become an input to a Temporal workflow, never an
alternative scheduler.

### Security policy and credential boundaries

Both systems use explicit mounts and OneCLI credential injection. Pynchy adds
the key controls NanoClaw lacks: service trust declarations, separate
corruption/secret taint, the lethal-trifecta gate, secrets scanning, and a
cross-core Bash security gate. Pynchy also scopes secrets and distinguishes
the deliberately trusted host execution mode from normal container execution.

NanoClaw's approval model is well factored, but it does not supersede this
policy. Any borrowed action catalog must invoke Pynchy's existing policy and
must not reduce decisions to a simple allow/hold/deny table.

### Semantic evidence, not just unit tests

Pynchy currently declares 77 user-meaningful actions. `pytest
--action-coverage` requires behavioral coverage for every action and its
canary contract adds independently verified real-service evidence where needed.
NanoClaw has many focused tests, including useful guard conformance tests, but
does not expose an equivalent semantic catalog or a real-service action gate.

Treat Pynchy's action catalog as the place to connect any NanoClaw-inspired
feature to its user outcome. A new attachment send is `message.attachment.send`
or similar—not merely another channel method and not a test that mocks an
upload call.

### Plugins and operator-owned configuration

NanoClaw's skills can copy selected channel/provider modules from long-lived
branches into a user's fork. That keeps a fresh install small, but makes update
compatibility an individual code-merge problem. Its own skill engine carefully
pins dependencies and makes each directive idempotent; that is good mechanics,
not a reason to abandon Pynchy plugins.

Pynchy's pluggy hooks, entry-point discovery, workspace specs, profile
composition, and config-over-plugin precedence keep extension ownership clear.
An optional integration should stay an installable plugin or a selected MCP
spec, not become a local source-code patch that the next update must preserve.

### Queueing, channel reconciliation, and broadcast semantics

Pynchy serializes work per group, limits global concurrent containers, lets a
live turn receive a safe follow-up via IPC, and prioritizes human messages over
scheduled work. Its outbound ledger has ordered reconciliation retries. Its
canonical-group broadcast model intentionally makes a conversation visible on
each configured surface.

NanoClaw's session drain guard avoids duplicate sends when its one-second and
one-minute delivery loops overlap. That is a good invariant, but Pynchy already
addresses the same class through its queue and durable outbound ledger. The
remaining Pynchy delivery enhancement is provider-confirmed delivery receipts,
not a replacement delivery poller.

## High-value gaps and patterns to adopt

### Implemented foundation: typed host-action catalog

Pynchy now parses service-handler contributions into immutable
`HostActionDescriptor` values and rejects duplicate identities, missing
ActionSpecs, unsafe write idempotency, and incomplete terminal-audit contracts
at startup. Dispatch, approval replay, capability status, and security audit
all consume the same catalog. See
[Action coverage](../../docs/architecture/action-coverage.md#host-action-descriptors-and-capability-status).

### P0: Add a generic attachment and outbox protocol

NanoClaw treats media as a first-class message result. The agent calls
`send_file`; the runner moves a file under an outbox directory named by the
outbound message ID; the SQLite row contains only safe filenames; the host
reads/uploads the files and deletes the outbox only after delivery. The source
also defends its inbound attachment store against crafted names and symlinks.

Pynchy's protocol currently exposes text/trace/result events plus an untyped
metadata bag. Its own channel guide explicitly calls out inbound Discord audio
transcription while listing outbound voice and interactive questions as absent.
That blocks TTS, generated reports, images, audio replies, and a portable
approval/question UI.

Add an owned attachment model before channel-specific features:

```python
@dataclass(frozen=True)
class MediaAttachment:
    filename: SafeFilename
    content_type: str
    data_path: Path
    disposition: Literal["file", "image", "audio", "voice"]

@dataclass(frozen=True)
class OutboundMessage:
    text: str
    attachments: tuple[MediaAttachment, ...] = ()
    interaction: OutboundInteraction | None = None
```

The host, not a channel formatter, should own staging, path containment,
exclusive creation, hashes, cleanup, and the delivery ledger. A channel then
declares attachment and interaction capabilities and either delivers natively
or returns a deliberate degradation result. Add `confirm_outbound` to the
channel contract at the same time so a send acceptance is not confused with a
provider-confirmed receipt.

Start with a file/image contract and explicit text fallback. TTS and voice
notes become simple producers of `MediaAttachment`, not separate channel
features. This also gives Pynchy a safe path to the existing vault's voice/TTS
recommendation.

### P1: Make session topology explicit without discarding canonical groups

NanoClaw separates the durable identities of messaging group, agent group, and
session. It can choose one session per channel, one per thread, or one shared
agent session across channels. It also preserves the source channel instance
on a reply, avoiding accidental delivery via a sibling connection with the
same platform address.

Pynchy already handles the valuable special case: Discord threads are isolated
dynamic workspaces inheriting their parent profile. Its canonical group model
also intentionally broadcasts one conversation across connected channels. The
gap is that these are policies embedded in routing rather than an explicit,
reviewable contract that any channel can choose.

Introduce a small `SessionTopology` resolver at the workspace/channel
boundary, with modes such as:

- `canonical_group` — Pynchy's current multi-channel shared conversation;
- `per_thread` — generalize the existing Discord-thread behavior; and
- `isolated_task` — Pynchy's existing scheduled-task session behavior.

Do **not** add NanoClaw's `agent_shared` mode by default. Sharing a transcript
across unrelated chats changes the data-isolation promise and should require a
user-visible, explicit workspace relationship. Likewise, preserve Pynchy's
broadcast behavior where configured rather than silently changing reply
delivery to origin-only.

### P1: Build core-neutral handoff on Temporal, not provider-specific teams

NanoClaw models agent-to-agent traffic as ordinary durable messages with an
explicit destination ACL, an optional approval policy for each edge, a source
session return path, and a host routing module. That is a clean pattern for
delegation because the core sees a constrained message contract rather than
another provider's opaque subagent feature.

Pynchy deliberately keeps native Teams disabled while per-teammate transcript
isolation remains unsolved. That restriction is correct. Re-enabling Teams
because NanoClaw has agent-to-agent messaging would reintroduce the same
transcript-safety problem under another name.

Instead, define a durable `Handoff` record with source/target workspace,
bounded input artifact references, requested result schema, cancellation,
approval state, and terminal result. Execute it through a Temporal workflow
and emit the result as a normal Pynchy event. Start with same-owner, explicit
workspace destinations; make cross-trust-boundary handoffs approval-gated.
Claude `Task`, Codex delegation, and future cores can become optional backends
behind that contract, rather than defining Pynchy's collaboration semantics.

### P1: Add script-gated scheduled tasks as a safe preflight

NanoClaw can attach a script to a scheduled task. The script decides whether
work exists before the agent is woken, which avoids spending a model turn on a
daily task with nothing to report. It still records a run log and keeps the
task's output delivery separate from the final agent response.

Pynchy's task model already has the harder parts—isolated task sessions,
Temporal scheduling, durable checkpoints, and explicit `send_message`. Add a
limited preflight activity only after the host-action descriptor exists:

- run an allowlisted, timeout-bounded executable in the task's existing
  container/worktree boundary;
- parse a typed result such as `skip`, `run`, or `run_with_context`;
- record the preflight outcome as a task event and action; and
- never let the preflight mutate a host configuration or send an external
  message by itself.

This should be an opt-in cost-control capability, not an implicit prompt
heuristic.

### P1: Use executable setup plans for integrations, while keeping TOML

NanoClaw's most elegant product pattern is not fork mutation; it is its
structured `nc:` skill directives. A normal `SKILL.md` remains useful prose,
while optional fenced directives declare idempotent copies, exact dependency
pins, secret-shaped prompts, validation, operator steps, restart effects, and
postcondition checks. The engine declares needs and emits events; a wizard,
chat relay, or CI pipeline owns prompting and presentation. Unsupported steps
degrade to an agent task rather than silently disappearing.

Pynchy's integration setup currently relies on configuration and separate
guides. Add a small, Pynchy-specific setup-plan runner for integrations that
need pairing, OAuth, or a controlled secret handoff. It should:

- consume a typed plan beside a plugin, not arbitrary shell prose;
- declare side effects and require preconditions/postconditions;
- obtain secret material through the existing OneCLI/secret path;
- show a dry-run plan, journal completed idempotent steps, and surface a
  resumable failure; and
- leave ownership of Pynchy configuration with the operator.

Keep TOML as the final desired-state source of truth. The plan runner prepares
or validates that desired state; it must not evolve into a source-code copier
or a hidden second configuration language.

### P2: Improve recovery ergonomics, tailored to managed deployment

NanoClaw uses a persisted crash-start circuit breaker and an upgrade marker.
The marker makes a raw `git pull` fail at startup because migrations, rebuilds,
and setup changes may have been skipped. This is sensible for a hand-managed
fork, but Pynchy's normal deployment path reconciles `origin/main` and
configuration automatically. A hard refusal after every unexpected Git state
would make the managed path more brittle.

Adapt the intent rather than the mechanism:

- add `pynchy doctor` with read-only checks for config/schema compatibility,
  plugin discovery, container runtime, gateway readiness, OneCLI material,
  Temporal health, channel liveness, pending approvals, and outbound backlog;
- write a deployment compatibility record only after Pynchy's managed
  migration/reconciliation transaction completes; and
- surface a mismatch in `/status` and block only the unsafe subsystem, with a
  concrete recovery command, rather than refusing the entire service to boot.

This matches Pynchy's operator model while retaining NanoClaw's useful "do not
pretend an interrupted upgrade succeeded" invariant.

## Specific design lessons

### Single-writer ownership is the important idea, not two SQLite files

NanoClaw's split database architecture has unusually crisp invariants: the
host owns `inbound.db`, the container owns `outbound.db`, neither writes the
other's file, and the host opens/writes/closes to make guest mounts observe
changes. Its documented decision to avoid WAL on VirtioFS is a good example of
recording a surprising platform constraint at the boundary that needs it.

Pynchy's file IPC serves a different lifecycle and already has idempotent
request claiming. Do not force an SQLite swap. Instead, make ownership,
idempotency, cleanup, and recovery rules equally explicit for every Pynchy IPC
directory and durable ledger. Add a short protocol ownership table to the IPC
architecture guide when the next IPC shape changes.

### Model outcomes separately from delivery

NanoClaw records a task result, an outbound message, and a delivered/failed
receipt separately. Pynchy similarly has an outbound ledger and reconciliation
retry. The shared lesson is to keep these terminal states distinct:

`agent execution` → `external action result` → `delivery accepted` →
`provider receipt`.

The current Pynchy ledger reaches delivery accepted when `send_event` returns.
The attachment work should add explicit unknown-outcome and confirmed-receipt
states rather than treating retry exhaustion as proof that no remote action
occurred.

### Bind approvals to the live operation, then recheck on replay

NanoClaw approval grants include an action name and can bind to request details
such as a destination. On replay the guard runs live structural checks again:
an old approval never overrides a later revoked destination relationship.

This is a good strengthening for Pynchy's existing approval files. Approval
payloads should bind the action ID, actor/group, normalized target, content
hash or immutable artifact ID, expiry, and idempotency key. The current
security policy remains responsible for taint and secret checks at replay time.

### Make capabilities queryable, then degrade deliberately

NanoClaw's Chat SDK bridge has channel-specific rich capabilities but exposes
one adapter surface, with text fallback when a channel cannot render a card.
Pynchy's untyped outbound metadata asks each formatter to infer what it can
do. A `ChannelCapabilities` object would let the host decide intentionally:
whether a destination accepts files, images, audio, voice, reactions, edits,
buttons, or provider delivery receipts.

This should share the runtime capability inventory proposed in the OpenClaw
comparison, not become channel-only metadata. It enables both honest user
responses ("sent the file as a link because this channel cannot upload it") and
capability-aware planning before an agent creates an artifact.

### Separate deterministic setup from conversational recovery

NanoClaw's installer runs the deterministic path first and hands a failure to
Claude Code for judgment. Its structured skill format preserves the same plan
for an interactive wizard, an agent relay, and noninteractive pipeline.

Pynchy should use the same boundary: deterministic checks and state changes
belong in a typed plan; diagnosis and repair suggestions may use an agent but
must report the failed check and must not silently invent a migration. This is
particularly useful for channels and OAuth setup, where configuration errors
otherwise look like generic connection failures.

## Capabilities to defer until a concrete workflow justifies them

- **Channel count.** NanoClaw's optional adapter catalog is useful discovery,
  but a Pynchy channel should land only with a maintained plugin, action
  coverage, security profile, setup path, and a concrete user workflow. Mail,
  calendar, document writes, and outbound media provide more immediate value
  than copying every chat surface.
- **Interactive cards everywhere.** Start with a portable question/approval
  model and text fallback. Rich Slack/Discord controls can follow once the
  attachment/outbox and capability contracts exist.
- **Self-modifying agent runtime.** NanoClaw can approve package and MCP
  changes that rebuild a group image. Pynchy should keep code/plugin changes in
  versioned worktrees and make runtime configuration changes explicit
  operator-owned state. Container rebuild permission is not enough review for
  a supply-chain change.
- **NanoClaw's sweep scheduler or two-DB migration.** Neither improves on
  Temporal plus Pynchy's queue and existing IPC idempotency.
- **A default cross-channel shared transcript.** Pynchy's canonical group
  sharing remains a conscious configuration; a global agent-shared mode would
  blur unrelated conversations and weaken isolation expectations.

## Recommended dependency order

1. **Capability and host-action descriptors.** Replace raw handler metadata
   where it crosses a security boundary; add the completeness/conformance
   checks. This establishes one vocabulary for approvals, audit, tests, and
   operator status.
2. **External action/delivery lifecycle.** Extend the outbound ledger with
   action IDs, idempotency keys, accepted/confirmed/unknown outcomes, and
   channel capability discovery.
3. **Attachment/outbox vertical slice.** Deliver one file/image through one
   channel with path containment, durable cleanup, provider receipt support,
   hermetic coverage, and a canary. Add TTS/voice only as a producer after this
   works.
4. **Core-neutral handoff and task preflight.** Model handoffs and preflights
   through Temporal using the descriptor/lifecycle contracts. Keep native Teams
   disabled until isolation is proven through this path.
5. **Setup plan and doctor.** Reuse descriptor capabilities to make a
   resumable setup plan and truthful operational diagnosis, without duplicating
   configuration ownership.
6. **General session topology.** Implement only after a real channel/workflow
   needs an isolation mode Pynchy cannot express today; migration must preserve
   existing canonical-group routing by default.

## Evidence pointers

### Pynchy

- `docs/architecture/container-isolation.md` and
  `docs/architecture/security.md` — explicit mounts, runtime plugins,
  LLM/OneCLI credential boundaries, taint policy, and host-mode exception.
- `docs/architecture/message-routing.md`,
  `src/pynchy/host/orchestrator/concurrency.py`, and
  `src/pynchy/host/orchestrator/temporal/` — queueing, durable turn recovery,
  workflows, schedules, and retries.
- `src/pynchy/plugins/hookspecs.py`, `src/pynchy/plugins/registry.py`, and
  `docs/architecture/workspaces.md` — plugin contracts, profile composition,
  config precedence, and dynamic thread workspaces.
- `src/pynchy/types.py` and `docs/usage/channels.md` — current text-event
  channel contract; existing streaming/reactions/inbound STT; explicitly absent
  outbound voice and interactive questions.
- `src/pynchy/state/outbound.py`,
  `src/pynchy/host/orchestrator/messaging/sender.py`, and
  `src/pynchy/host/orchestrator/messaging/reconciler.py` — outbound ledger and
  ordered retry reconciliation.
- `src/pynchy/actions.py` and `docs/architecture/action-coverage.md` — the
  77-action semantic test/canary contract.
- `docs/architecture/observers.md` — existing pluggable observation surface.

### NanoClaw

- `README.md`, `docs/architecture.md`, `src/router.ts`, and
  `src/session-manager.ts` — entity routing, session modes, two-DB protocol,
  container lifecycle, and attachment/outbox design.
- `src/delivery.ts`, `src/db/session-db.ts`, `src/host-sweep.ts`, and
  `src/circuit-breaker.ts` — delivery claims, retry handling, polling,
  recurrence, stale-turn recovery, and startup backoff.
- `src/guard/`, `src/delivery-guard.ts`, `src/modules/approvals/`, and
  `src/modules/agent-to-agent/` — guarded-action catalog, approval replay,
  destination ACLs, and durable agent-to-agent routing.
- `container/agent-runner/src/mcp-tools/`, `docs/scheduled-tasks.md`, and
  `container/agent-runner/src/destinations.ts` — message/file/tool contracts,
  script-gated task work, explicit destinations, and isolated task sessions.
- `docs/skill-directives.md` and `docs/skill-engine-seam.md` — prose-primary,
  idempotent setup directives and the declaration/presentation boundary.
- `docs/upgrade-recovery.md` and `src/upgrade-state.ts` — upgrade state
  assertion and recovery behavior.
