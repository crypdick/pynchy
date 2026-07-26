---
source_commit: 9a2f2c673af291e482987647b53b4f51245c6f00
generated_at: 2026-07-26T21:27:13Z
graphify_version: 0.9.26
graph_mode: full-src-undirected-clustered
semantic_model: Codex delegated extraction (model identifier not recorded)
policy_version: 1
---

# Pynchy graph report

> [!WARNING]
> This manually reviewed report describes `src/` at the source commit above. It
> may become stale. Treat source code and typed contracts as authoritative;
> treat Graphify communities, centrality, inferred edges, and recommendations as
> navigation aids. See `docs/architecture/service-boundaries-roadmap.md` for the
> canonical architecture plan and refresh policy.

## Refresh contract

When refreshing this snapshot:

- describe the selected source commit's current state, not the work history;
- remove resolved findings and consolidate repeated conclusions;
- retain useful symbols and source paths when they save future discovery work;
- distinguish `EXTRACTED`, `INFERRED`, and `AMBIGUOUS` evidence;
- include dated, sanitized operational behavior only when it affects other
  users; and
- omit private issue IDs, host or channel identifiers, account and workspace
  names, personal paths, IP addresses, credentials, and identifying raw logs.

## Corpus and extraction

The run covered the complete selected corpus, not an incremental changed-file
batch:

| Measure | Result |
|---|---:|
| Detected files | 547 |
| Code files | 543 |
| Documents | 4 |
| Approximate words | 288,175 |
| Structural nodes | 9,401 |
| Structural edges | 28,770 |
| Semantic nodes | 39 |
| Semantic edges | 45 |
| Semantic hyperedges | 2 |
| Merged nodes | 9,440 |
| Raw merged edges | 28,815 |
| Undirected graph edges | 25,474 |
| Communities | 280 |
| Semantic input tokens | 1,814 |
| Semantic output tokens | 6,440 |

The graph marks about 89% of built edges `EXTRACTED` and 11% `INFERRED`; it
contains no `AMBIGUOUS` edges. Absence of ambiguity labels does not make
inferred edges authoritative.

## Graph health

Graphify's read-only diagnostic reported:

| Diagnostic | Count | Interpretation |
|---|---:|---|
| Dangling-endpoint edges | 2,017 | Mostly imports whose external module has no graph node; includes 18 known checkout-path caller IDs |
| Missing-endpoint edges | 0 | Every edge has source and target fields |
| Self-loop edges | 2 | Package `__init__` imports collapsed to their own file node |
| Exact duplicate edges | 362 | Repeated structural facts before graph construction |
| Directed same-endpoint collapses | 1,253 | Distinct raw edges would share one ordered endpoint pair |
| Undirected same-endpoint collapses | 1,325 | Distinct raw edges share one endpoint pair in this run's undirected graph |
| Relation-variant groups | 635 | Endpoint pairs carry more than one relation kind |

These warnings constrain interpretation:

- `AMBIGUOUS`: The 2,017 dangling edges do not prove broken source
  dependencies. Most point to standard-library or third-party modules that the
  corpus does not represent as nodes.
- `EXTRACTED`: Eighteen `indirect_call` edges use a checkout-derived caller ID.
  The repository wrapper narrowly canonicalizes this known Graphify 0.9.26
  defect in the tracked raw graph.
- `EXTRACTED`: The two self-loops come from `pynchy/__init__.py` and
  `pynchy/agent/agent_runner/src/agent_runner/__main__.py`; they reflect
  extractor identity collapse, not application recursion.
- `INFERRED`: Edge and community counts understate distinct source
  relationships because the undirected graph collapses multiedges.

Do not turn these counts into hook thresholds or package-boundary rules.

## High-volume communities

Community IDs belong only to this snapshot and can change on the next manual
run. Labels summarize the largest clusters for navigation.

| ID | Label | Nodes | Cohesion | Primary source areas |
|---:|---|---:|---:|---|
| 0 | Capability and action contracts | 123 | 0.06 | `pynchy/capabilities.py`, `pynchy/plugins/integrations/gog/_plugin.py` |
| 1 | Matrix connection lifecycle | 121 | 0.04 | `pynchy/plugins/integrations/matrix_gateway_client.py`, `matrix_connection.py` |
| 2 | Container IPC dispatch | 114 | 0.04 | `pynchy/host/container_manager/ipc/watcher.py`, `ipc/deps.py` |
| 3 | Work-item persistence | 112 | 0.05 | `pynchy/plugins/integrations/linear_work_item_provider.py`, `pynchy/state/work_items.py` |
| 4 | Messaging control dependencies | 106 | 0.04 | `pynchy/host/orchestrator/messaging/deps.py`, `messaging/host_controls.py` |
| 5 | Temporal workflow scheduling | 101 | 0.04 | `pynchy/host/orchestrator/temporal/workflows.py`, `temporal/scheduler.py` |
| 6 | Stateful turn persistence | 101 | 0.04 | `pynchy/state/in_flight_turns.py`, `pynchy/state/tasks.py` |
| 7 | Temporal schedule reconciliation | 101 | 0.05 | `pynchy/host/orchestrator/temporal/schedules.py`, `schedule_reconciler.py` |
| 8 | Agent execution lifecycle | 100 | 0.04 | `pynchy/host/orchestrator/agent_runner.py`, `host_execution.py` |
| 9 | Proton Mail integration | 95 | 0.05 | `pynchy/plugins/integrations/proton_mail.py`, `pynchy/config/mcp.py` |
| 10 | Mount security policy | 91 | 0.05 | `pynchy/host/container_manager/security/mount_security.py`, `pynchy/types.py` |
| 11 | Linear client integration | 87 | 0.05 | `pynchy/plugins/integrations/linear_client.py`, `pynchy/plugins/integrations/linear.py` |
| 12 | Temporal runtime activities | 85 | 0.04 | `pynchy/host/orchestrator/temporal/runtime_state.py`, `temporal/scheduler.py` |
| 13 | Linear board integration | 85 | 0.06 | `pynchy/plugins/integrations/linear_boards.py`, `linear_workspace_names.py` |
| 14 | Agent runner core | 84 | 0.05 | `pynchy/agent/agent_runner/src/agent_runner/main.py`, `host_direct.py` |
| 15 | Configuration models | 82 | 0.09 | `pynchy/config/models.py`, `pynchy/config/workspace_layout.py` |
| 16 | Orchestrator dependency adapters | 80 | 0.04 | `pynchy/host/orchestrator/adapters.py`, `pynchy/host/orchestrator/dep_factory.py` |
| 17 | Discord inbound events | 79 | 0.05 | `pynchy/plugins/channels/discord/_events.py`, `discord/_models.py` |
| 18 | Work-item task execution | 78 | 0.07 | `pynchy/plugins/integrations/linear_work_item_tasks.py`, `linear_decision_inbox.py` |
| 19 | IPC service security handlers | 77 | 0.05 | `pynchy/host/container_manager/ipc/handlers_service.py`, `handlers_security.py` |

Low cohesion is a prompt to inspect a cluster, not proof of poor application
cohesion. Graph construction and shared foundational types influence these
scores.

## God nodes

These graph hubs help agents find cross-cutting contracts. Their degree mixes
extracted and inferred relationships.

| Symbol | Degree | Source |
|---|---:|---|
| `WorkspaceProfile` | 321 | `pynchy/types.py` |
| `NewMessage` | 155 | `pynchy/types.py` |
| `Channel` | 149 | `pynchy/types.py` |
| `Settings` | 140 | `pynchy/config/settings.py` |
| `OutboundEvent` | 139 | `pynchy/types.py` |
| `_get_db()` | 123 | `pynchy/state/connection.py` |
| `ScheduledTask` | 122 | `pynchy/types.py` |
| `IpcDeps` | 100 | `pynchy/host/container_manager/ipc/deps.py` |
| `PynchyApp` | 97 | `pynchy/host/orchestrator/app.py` |
| `ServiceTrustConfig` | 95 | `pynchy/types.py` |

`INFERRED`: The concentration of `WorkspaceProfile`, `NewMessage`, `Channel`,
`OutboundEvent`, `ScheduledTask`, and `ServiceTrustConfig` in
`pynchy/types.py` supports reviewing that module by ownership. Centrality alone
does not justify splitting a stable contract.

## Agent navigation index

### Inbound messages and delivery

- `NewMessage` and `OutboundEvent`: `pynchy/types.py`
- `_turn_id_for_batch()`, `_input_source_for_batch()`, and
  `_conversation_claim_for_batch()`:
  `pynchy/host/orchestrator/messaging/pipeline.py`
- `MessageHandlerDeps`:
  `pynchy/host/orchestrator/messaging/deps.py`
- inbound reconciliation:
  `pynchy/host/orchestrator/messaging/reconciler.py`
- routing and streamed output:
  `pynchy/host/orchestrator/messaging/router.py` and `streaming.py`
- delivery persistence: `pynchy/state/outbound.py` and
  `pynchy/state/conversation_routing.py`

### Agent execution

- host orchestration: `pynchy/host/orchestrator/agent_runner.py` and
  `host_execution.py`
- container runner entry point:
  `pynchy/agent/agent_runner/src/agent_runner/main.py`
- host-direct consumer:
  `pynchy/agent/agent_runner/src/agent_runner/host_direct.py`
- provider contract and `AgentEvent`:
  `pynchy/agent/agent_runner/src/agent_runner/core.py`
- Codex provider:
  `pynchy/agent/agent_runner/src/agent_runner/cores/codex.py`

### Scheduling and lifecycle

- scheduler dependency surface:
  `pynchy/host/orchestrator/scheduler_deps.py`
- dependency construction: `pynchy/host/orchestrator/dep_factory.py`
- task execution: `pynchy/host/orchestrator/task_scheduler.py`
- Temporal workflows and activities:
  `pynchy/host/orchestrator/temporal/workflows.py`,
  `scheduler.py`, and `runtime_state.py`
- schedule reconciliation:
  `pynchy/host/orchestrator/temporal/schedule_reconciler.py`

### State and identity

- database lifecycle: `pynchy/state/connection.py`
- interactive checkpoints: `pynchy/state/in_flight_turns.py`
- scheduled tasks: `pynchy/state/tasks.py`
- action intents: `pynchy/state/action_intents.py` and
  `pynchy/action_intents.py`
- conversation identity: `pynchy/conversation/models.py`

### Extensions and security

- plugin API and registration: `pynchy/plugins/`
- concrete channel adapters: `pynchy/plugins/channels/`
- concrete integrations: `pynchy/plugins/integrations/`
- container security:
  `pynchy/host/container_manager/security/`
- service approval dispatch:
  `pynchy/host/container_manager/ipc/handlers_service.py`
- core configuration: `pynchy/config/settings.py` and
  `pynchy/config/models.py`

## Current architectural recommendations

### 1. Split contracts by ownership, not graph degree

`EXTRACTED`: Six of the ten highest-degree symbols live in
`pynchy/types.py`. That module includes workspace and trust configuration,
message transport models, scheduled work, container output, presentation
events, and the `Channel` adapter protocol.

`INFERRED`: Introduce ownership-focused modules as those contracts change:
conversation and turn identity, security provenance, scheduling, wire models,
and outbound presentation. Preserve deliberate public re-exports during the
migration. Do not ban central stable contracts or set degree thresholds.

### 2. Replace broad dependency facades with use-case capabilities

`EXTRACTED`: `MessageHandlerDeps` exposes channels, workspaces, queue state,
session controls, deployment, delivery, agent execution, output handling, and
event emission. `SchedulerDependencies` exposes a similar cross-section, while
`make_scheduler_deps()` casts the complete `PynchyApp` to that protocol.

`INFERRED`: Split these surfaces into narrow capabilities such as turn
execution, delivery publication, checkpoint persistence, and session reset.
Keep `PynchyApp` as the composition root rather than a general service locator.

### 3. Move security and lifecycle identity out of message metadata

`EXTRACTED`: `messaging/pipeline.py` reads `turn_id`,
`authenticated_external_route`, `public_source_input`, `external_provider`,
and `conversation_claim_id` from `NewMessage.metadata`.

`INFERRED`: Parse provider input into a typed inbound envelope that carries
authenticated provenance and lifecycle identities together. Retain metadata
for optional transport and rendering details.

### 4. Enforce one provider event-stream protocol

`EXTRACTED`: `AgentEvent` remains `type: str` plus `data: dict[str, Any]` in
`agent_runner/core.py`. Provider cores construct these shapes independently,
and both container and host-direct paths consume them.

`INFERRED`: Introduce a discriminated event union and a shared stream validator
that enforces one terminal result, rejects post-result events and unknown
types, and turns a clean result-less EOF into a typed failure.

### 5. Invert configuration dependencies on concrete integrations

`EXTRACTED`: `pynchy/config/models.py` imports `LinearTool` and
`MatrixConnectionConfig` from concrete integration modules to construct core
discriminated unions.

`INFERRED`: Give integrations a registration contract for their configuration
models so core configuration does not import concrete plugins.

### 6. Inject state configuration

`EXTRACTED`: `pynchy/state/connection.init_database()` calls
`get_settings()` to discover the SQLite path.

`INFERRED`: Resolve the database path at bootstrap and pass it into state
initialization. This makes the adapter's configuration explicit and removes a
second ambient initialization path from tests.

### 7. Enforce source-level dependency direction

`EXTRACTED`: Large communities span orchestrator, state, concrete plugins,
Temporal, and container IPC. The apparent links identify candidates, but the
undirected graph loses edge multiplicity and some import-kind distinctions.

`INFERRED`: Enforce the positive component graph described in
`docs/architecture/service-boundaries-roadmap.md` with a deterministic AST
checker and a ratcheted baseline. Keep Graphify advisory; never derive blocking
rules from community IDs, degree, or inferred edges.

## Curated cross-community connections

Each connection below comes from Graphify's `INFERRED` analysis and requires
source confirmation:

- `_RuntimeState` uses `TurnOutcome` across
  `pynchy/host/orchestrator/temporal/runtime_state.py` and
  `pynchy/host/orchestrator/execution_outcomes.py`. This suggests a shared
  execution-outcome contract between Temporal state and interactive turns.
- `get_effective_action_specs()` calls `validate_action_specs()` across
  `pynchy/plugins/host_actions.py` and `pynchy/_action_contract.py`. This points
  to the action contract as an inward-facing extension boundary.
- `_ApprovalRequestContext` and `_ServiceRequest` use `ActionIntent` across
  `pynchy/host/container_manager/ipc/handlers_service.py` and
  `pynchy/action_intents.py`. This connects IPC approval transport to the
  durable external-action lifecycle.

The generated test-to-implementation connection for OpenAI tool parsing is
ordinary test coverage, so it does not produce an architectural
recommendation.

## Corrected cycle interpretation

Graphify reports a three-file Discord import cycle:

`_approval.py` → `_channel.py` → `_outbound.py` → `_approval.py`.

`EXTRACTED`: `_approval.py` imports `_channel.py` only under `TYPE_CHECKING`,
while `_channel.py` imports `_outbound.py` and `_outbound.py` imports
`_approval.py` at runtime.

`INFERRED`: Treat this as a typing/back-reference design smell, not a
three-module runtime cycle. A source-level boundary checker should report
runtime and type-checking edges separately.

## Semantic document flows

The four agent-skill documents produced two reviewed semantic hyperedges:

- `EXTRACTED`: Slack session setup, saved browser profile, token refresh, and
  browser tokens form a persistent-session refresh flow in
  `pynchy/agent/skills/slack-token-extractor/SKILL.md`.
- `EXTRACTED`: X session setup, saved browser profile, and posting interaction
  tools form a persistent-session action flow in
  `pynchy/agent/skills/x-integration/SKILL.md`.

The browser-control and computer-use documents also share `INFERRED`
relationships around snapshot references, untrusted visible content, and
observable mutation verification.

## Suggested questions

1. Which concrete imports from `pynchy/plugins/` reach state, Temporal,
   orchestrator, or container implementations, and which use-case-owned port
   should replace each one?
2. How do `turn_id`, provider trust, and `conversation_claim_id` enter
   `NewMessage.metadata`, and where should a typed inbound envelope first
   exist?
3. Which methods in `MessageHandlerDeps` belong to independent capabilities,
   and which call sites genuinely need more than one capability?
4. How does `AgentEvent` travel from every provider through the container and
   host-direct consumers, and where can duplicate or missing terminal results
   escape validation?
5. Which members of `pynchy/types.py` change for unrelated reasons, and which
   can remain stable public re-exports after ownership-focused splits?
6. How can plugin-owned configuration register with core parsing without
   importing concrete Linear or Matrix modules?
7. Which state and application modules still read ambient settings, and which
   values should their composition roots inject?
8. Which Graphify dangling or collapsed edges reflect extractor limitations,
   and which survive confirmation by the source AST?
