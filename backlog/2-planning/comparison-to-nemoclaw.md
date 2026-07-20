# Comparison to NVIDIA NemoClaw

## Conclusion

NemoClaw and Pynchy solve adjacent problems. NemoClaw packages selected agent
runtimes inside NVIDIA OpenShell and makes a hardened sandbox, egress policy,
inference route, and sandbox lifecycle operable through one CLI. Pynchy owns a
larger personal-assistant product: channel routing, workspace isolation,
profiles, host tools, durable scheduled work, plugin extensions, and multiple
agent cores.

Pynchy should not adopt NemoClaw as a second control plane or try to port its
OpenShell-specific implementation. Its strongest lessons are lower-level,
composable contracts that make Pynchy's existing design safer and easier to
operate:

1. Enforce deny-by-default egress below agent tools, then compose that policy
   with Pynchy's existing source/secret/sink taint policy.
2. Harden the production agent image as a reproducible, least-privilege
   artifact; Pynchy's current primary image deliberately grants passwordless
   `sudo` and downloads mutable runtime dependencies.
3. Treat runtime, channel, MCP, and provider support as declarative capability
   contracts with health, state, policy, and evidence requirements.
4. Give operators a read-only doctor, a real route probe, and safe
   workspace-state snapshots rather than asking them to assemble diagnostics
   from logs and config files.
5. Use provider/agent capability evidence and progressive tool disclosure to
   keep model-specific failures observable as the integration surface grows.

The right direction is therefore **Pynchy semantics and orchestration above a
stronger execution substrate**. Do not trade the former for a generic sandbox
manager.

## Scope and evidence

This report compares Pynchy `main` at
`85bcb76ef1ba43e9e7dd484f8ab556205eae3710` (2026-07-17) with a shallow
checkout of [NVIDIA/NemoClaw](https://github.com/NVIDIA/NemoClaw) at
`c9d60396f9b5e8b8a530729eb2d98db5a690b5cb` (2026-07-17). It uses source and
the repositories' maintained architecture and operations documentation as the
authority, not marketing claims.

NemoClaw is still an alpha reference stack and delegates its enforcement to
OpenShell. Findings below distinguish that platform dependency from patterns
Pynchy can implement against its Docker and Apple Container runtime choices.

Primary Pynchy evidence: `docs/architecture/security.md`,
`src/pynchy/host/container_manager/mounts.py`,
`src/pynchy/agent/Dockerfile`, `src/pynchy/plugins/hookspecs.py`,
`docs/usage/agent-cores.md`, `docs/usage/scheduled-tasks.md`,
`docs/architecture/action-coverage.md`, and `src/pynchy/canaries.py`.
Primary NemoClaw evidence: its [architecture reference](https://docs.nvidia.com/nemoclaw/latest/reference/architecture.html),
[security controls](https://docs.nvidia.com/nemoclaw/latest/security/best-practices.html),
`nemoclaw-blueprint/policies/openclaw-sandbox.yaml`, `agents/*/manifest.yaml`,
`docs/inference/model-capability-audit.mdx`, `docs/manage-sandboxes/`, and
`Dockerfile.base` at the recorded source revision.

## Architecture and capability matrix

| Concern | Pynchy today | NemoClaw today | Assessment |
| --- | --- | --- | --- |
| Product boundary | Channel-first personal assistant with groups, profiles, plugins, host tools, worktrees, and scheduled work. | Host CLI and versioned blueprint for sandboxing compatible agent products. | Different layers. **Do not replace Pynchy's control plane.** |
| Agent runtimes | Pluggy-discovered Claude SDK, Claude CLI, OpenAI, and Codex cores; profiles resolve a model and tools. | OpenClaw, Hermes, and Deep Agents integrations each declare binary, config, health, durable state, inference, and MCP support in a manifest. | **Adapt the manifest contract.** Pynchy has the more extensible public plugin seam but a thinner core descriptor. |
| Channels | First-party Slack, Discord, and WhatsApp, plus host Matrix gateway; canonical group routing and Discord-thread workspaces. | Manifest-first lifecycle support for Discord, Slack, Telegram, Teams, WeChat, and WhatsApp on selected agent runtimes. | Pynchy has stronger workspace semantics. **Adapt data-driven compatibility/lifecycle planning, not channel count.** |
| Isolation | Docker or Apple Container with explicit mounts; trusted admin workspaces may deliberately use host mode. | OpenShell sandbox with network namespace, seccomp, filesystem policy, optional Landlock, capability drops, resource limits, and non-root runtime. | **Material Pynchy gap for container mode.** Host mode must remain visibly exempt rather than pretending to be sandboxed. |
| Egress and credentials | LiteLLM isolates LLM keys; scoped native credentials and Bash/service tools use semantic taint and approval gates. | Default-deny egress, host/port/binary restrictions, optional L7 method/path/MCP rules, SSRF checks, and credential rewriting at the proxy. | **Complementary.** Add substrate egress enforcement; retain Pynchy's stronger semantic policy. |
| Scheduling | Temporal schedules/workflows, retries, interrupted-turn checkpointing, canary scheduling, and task isolation. | Sandbox-native runtime lifecycle; no comparable durable multi-workspace scheduler. | **Pynchy ahead.** Do not replace Temporal with sandbox lifecycle loops. |
| Provider operation | LiteLLM routing and startup validation for configured effective core routes; Phoenix traces and action canaries. | Onboarding probes the actual API surface, manages provider state, has a model/agent audit-matrix schema, and reports in-sandbox route health. | **Adapt evidence and probing contracts.** Pynchy has stronger real-service action evidence already. |
| MCP lifecycle | Plugin/config specifications, workspace instance resolution, readiness checks, and trust declarations. | Managed MCP registration, policy/provider/adapter reconciliation, lifecycle lock, and differential credential-resolution probe. | **Adapt the differential probe and explicit lifecycle transaction.** |
| State and recovery | SQLite backups, migration copies, persistent session dirs, and managed deployment rollback; no portable per-workspace export/restore contract. | Manifest-defined sanitized snapshots, restore rules, rebuild transaction, state-aware recovery, and lifecycle locks. | **Real Pynchy operator gap.** Build a narrower Pynchy state manifest, not a copy of sandbox snapshots. |
| Extensibility | Public Pluggy hooks and entry-point discovery for cores, channels, memory, runtime, tools, observers, and workspaces. | Internal trusted manifests; the public SDK remains intentionally unimplemented. | **Pynchy ahead.** Do not abandon public plugin ownership for internal-only catalogues. |
| Validation | Strict typing and pre-commit gates; semantic ActionSpecs and independent canaries; Phoenix retains LLM traces. | Extensive workflow-boundary tests, image/policy pin checks, provider probes, and a documented capability-audit template. | **Combine strengths.** Give Pynchy's ActionSpecs the missing execution-substrate and route evidence. |

## What Pynchy should preserve

### Semantic security, workspace isolation, and durable orchestration

Pynchy's `SecurityPolicy` understands a fact that network policy alone cannot:
an action can combine attacker-controlled input, secret data, and a public
sink. It tracks corruption and secret taint separately, gates dangerous
writes, uses the Cop and approval flow, and applies the same Bash gate across
agent cores. NemoClaw's egress policy cannot replace those decisions.

Likewise, Pynchy's per-workspace queues, isolated sessions, worktree handling,
and Temporal workflows already solve durable assistant concerns that NemoClaw
deliberately leaves to the hosted agent product. Preserve those ownership
boundaries.

### Public, typed extension ownership

NemoClaw's channel manifests are elegant because configuration, required
packages, credentials, policy presets, runtime assets, and rebuild behavior
compile into one plan. Its catalogue is nevertheless trusted internal code.
Pynchy's pluggy hooks and config-over-plugin precedence are a better base for
third-party ownership. Add stronger typed descriptors and validation to that
surface instead of making extensions private build-time patches.

### Semantic action coverage and independent canaries

NemoClaw has strong implementation-boundary tests, but Pynchy's ActionSpec
catalogue and canaries make a better product promise: a named user action gets
hermetic behavioral coverage and, where appropriate, independently verified
live evidence. New infrastructure should register with this catalogue rather
than create a parallel "sandbox healthy" definition.

## High-value adoption work

### P0: Introduce an execution-substrate policy for container workspaces

Pynchy currently relies on container isolation, explicit mounts, the agent
runner's Bash gate. Its normal agent `docker run` argv
does not set a network policy, capability drops, `no-new-privileges`, or
resource limits. The primary agent image also grants its `agent` user
passwordless `sudo`, so a compromised agent can install or alter local tools
inside the container.

Add a typed `ExecutionPolicy` selected by workspace profile and enforced by
the runtime provider. It should express:

- network mode and a default-deny egress backend;
- declared destination capabilities from the selected Pynchy tools/MCP
  servers, including read versus write operations where possible;
- non-root user, dropped capabilities, `no-new-privileges`, PID/CPU/memory
  limits, and read-only system paths; and
- a truthful posture result (`enforced`, `degraded`, or `unsupported`) per
  runtime and host platform.

Start with a Linux/Docker backend that can enforce an allowlist. Integrate
the egress boundary through an enforceable runtime policy. Apple Container
needs a separate capability probe and must report degradation honestly if it
cannot enforce an equivalent rule. Direct host execution remains an explicit
trusted exception and should never receive a misleading secure posture.

Acceptance evidence: an untrusted container cannot connect to an unlisted
host even through `curl`, Python, Node, or a tool subprocess; a configured
read-only endpoint cannot issue a write; the host reports the actual policy
revision and enforcement mode; and each outcome maps to ActionSpecs and a
runtime integration test.

### P0: Make the standard agent image reproducible and least-privilege

NemoClaw pins base images by digest, packages by exact versions, downloaded
binaries by checksums, and rejects mutable production build inputs. Pynchy's
deterministic test image has good pins, but the production agent image uses
`python:3.13-slim`, `uv:latest`, global unpinned npm installs, a shallow plugin
clone, remote setup scripts, and passwordless `sudo`.

Create a locked production image contract:

- pin base images and externally fetched CLI/plugin artifacts by digest or
  version plus checksum;
- lock Python and npm dependencies, generate an SBOM, and fail CI on mutable
  production references;
- split a deliberately permissive developer/tool-installer image from the
  default runtime image; and
- remove passwordless `sudo` from the default container. Tool installation
  should become an operator-approved image/plugin change or a separately
  confined build action.

This has a usability cost for ad-hoc coding agents, but it is a genuine
security boundary, not incidental hardening. A profile may opt into a clearly
labelled developer image; non-admin conversational workspaces should not.

### P1: Replace thin core facts with runtime capability manifests

Evolve `pynchy_agent_core_info()` into an owned typed descriptor while keeping
the existing plugin API as an adapter during migration. The descriptor should
declare the supported configuration schema, health probe, session/state paths,
safe persistence/export rules, inference wire APIs, tool-discovery mode,
MCP adapter, channel compatibility, and execution-policy requirements.

Apply the same plan compiler idea to channels and MCP servers: a channel
declaration should state which cores it supports, configuration prerequisites,
network/credential requirements, lifecycle steps, health check, and
degradation behavior. Rebuild or deploy must re-resolve a compact persisted
desired state against current declarations instead of trusting stale derived
data.

This directly supports the existing plugin architecture and prevents a
configuration that appears valid but fails only after tearing down a running
session.

### P1: Add `pynchy doctor` and evidence-backed route/MCP probes

NemoClaw has a coherent operator story: status probes from inside the sandbox,
`doctor` checks supported runtime facts, and its MCP probe distinguishes an
opaque credential placeholder from a real proxy rewrite without printing the
secret or response body.

Add a read-only `pynchy doctor --json` that reports, without restarting
anything:

- resolved workspace/core/channel/MCP capability plan and incompatibilities;
- runtime posture, mount policy, queue/backlog, Temporal health, and pending
  approval state;
- the exact effective LiteLLM route and a core-appropriate live wire probe;
- MCP health plus an opt-in, idempotent credential-resolution differential
  probe for integrations that declare one; and
- stale deployment compatibility records, canary regressions, and precise
  remediation commands.

Make probe results structured evidence with commit/config digests and expiry.
Connect them to the existing action canaries and Phoenix correlation rather
than building another dashboard database.

### P1: Define portable workspace-state snapshots and recovery transactions

Pynchy already backs up SQLite databases and preserves migration copies, but
operators cannot ask for a sanitized, scoped export of one workspace's durable
session, configuration projection, approved skills, and state metadata. That
makes recovery across runtime/image changes unnecessarily manual.

Create a `WorkspaceStateManifest` owned by each core/plugin. It must enumerate
what to copy, merge, regenerate, redact, and never export; raw credentials,
mount allowlists, and host secrets remain excluded. A restore
should validate compatibility before replacement, stage a backup, apply an
idempotent transaction, re-probe the core and MCP plan, and retain the prior
state until verification passes. Use the existing managed deploy transaction
and Temporal where it owns asynchronous work; do not introduce an independent
sandbox registry.

### P2: Add progressive tool disclosure as a core-neutral capability

NemoClaw treats tool search as a context-budget feature, not an authorization
mechanism. It defines per-core discovery implementations, bounded search
results and schemas, and model-specific exceptions. Pynchy already provides
skill discovery and each core's native tool semantics, but it does not expose a
single declared tool-discovery capability or a compatibility/evidence matrix.

Add an optional `ToolDiscoveryPolicy` to the core manifest. Preserve direct
exposure for small tool sets and for cores where discovery degrades structured
calls. For large MCP sets, provide bounded keyword discovery with per-tool
schema limits and show omitted matches. Audit each core/model/route combination
through the same evidence model as provider validation.

### P2: Use declarative setup and lifecycle plans for complex integrations

NemoClaw's versioned blueprint offers a useful pattern: resolve, verify,
plan, apply, status, and rollback. Pynchy should use the pattern for its own
configuration and plugin lifecycle, not add a second YAML control plane.

Define plugin-owned setup plans for OAuth, pairing, browser profiles, MCP
provisioning, and external policy changes. A plan declares preconditions,
secret handoff boundary, idempotent steps, postconditions, restart impact, and
rollback/repair instructions. TOML remains desired state; the plan prepares,
validates, or reconciles it. Agent prose and arbitrary shell commands must not
become an implicit privileged setup language.

## Attractive ideas to avoid

- **Do not adopt OpenShell as a mandatory Pynchy runtime.** It would make a
  third-party platform the root of Pynchy's portability story and duplicate
  the existing runtime plugin seam. Consume comparable controls behind that
  seam where useful.
- **Do not replace service taint policy with host allowlists.** Egress rules
  cannot decide whether a public source plus a secret can safely reach a
  public sink. They are a defense-in-depth layer.
- **Do not copy mutable sandbox skills/plugins or generic agent self-install.**
  Runtime mutation undermines image provenance and policy attestation. Keep
  user choice in profiles, selected skills, plugins, and reviewed images.
- **Do not force whole-sandbox immutability (`shields`) on normal Pynchy
  workspaces.** Pynchy deliberately mounts worktrees, session homes, and
  selected vault skills. First identify Pynchy-owned immutable runtime paths
  and leave workspace data writable by contract.
- **Do not turn snapshot restore into cross-workspace history access.**
  Exports remain workspace-scoped and redacted; sharing stays an explicit
  handoff, not a recovery side effect.
- **Do not add a competing scheduler or global sandbox registry.** Temporal,
  the workspace config, and Pynchy state remain authoritative.

## Recommended sequencing

1. Deliver the locked production image and runtime posture reporting together;
   an unpinned image cannot provide a meaningful security attestation.
2. Add Linux/Docker default-deny egress and make its relationship to
   `SecurityPolicy` explicit. Follow with Apple Container support only when
   its enforcement contract is testable.
3. Introduce the typed core/channel/MCP capability descriptor and implement
   `pynchy doctor` against it. Pilot it on the Codex core and one MCP tool.
4. Add provider/model/route evidence rows and the differential MCP probe,
   reusing ActionSpecs, canaries, and Phoenix correlation.
5. Define `WorkspaceStateManifest` and implement snapshot/recovery for one
   core before expanding to every plugin.
6. Add tool discovery and setup plans only after their core and lifecycle
   contracts exist.

## Verification performed

- Inspected Pynchy architecture, security, container invocation, image build,
  plugin hooks, agent-core configuration, MCP lifecycle, Temporal scheduling,
  action coverage, canaries, and operational status surfaces at the recorded
  commit.
- Inspected NemoClaw architecture, OpenShell policy/credential boundary,
  default sandbox policy, image hardening, agent manifests, manifest-first
  channel lifecycle, inference validation, model audit schema, MCP lifecycle,
  snapshots/rebuild, tool disclosure, and operator commands at the recorded
  commit.
- Reviewed the immediately preceding Pynchy comparison records so repeated
  recommendations (attachments, core-neutral handoff, typed capabilities) are
  explicitly consolidated rather than proposed as competing designs.

## Key takeaways

- Pynchy needs lower-level enforcement and reproducible production artifacts
  more than it needs another agent platform or more adapters.
- Keep Pynchy's semantic security and Temporal workflow ownership; layer
  substrate controls underneath them.
- Make capability availability, effective policy, and real route behavior
  inspectable before adding more integration surface.
- Treat recovery, provider validation, and credential rewriting as verifiable
  state transitions with redacted evidence.
