# Affected-Workspace Runtime Policy Refresh

**Status:** Implemented.

**Outcome:** Refresh supported workspace policy without restarting the host,
retiring only affected sessions when a fresh runtime is required.

## Why this is separate

Resetting the settings cache does not update every consumer. Pynchy also builds
startup-owned runtime values for agent execution, queue policy, mounts,
security, tools, MCP, repositories, learning, and scheduler behavior. A generic
"settings hot reload" would therefore be incomplete and could apply sensitive
policy inconsistently.

## Scope

This brief classifies workspace runtime policy after profile composition. It
also classifies the referenced global definitions when a workspace field alone
does not describe the full policy.

The following changes stay with their dedicated reconciliation work:

- workspace, thread, profile-assignment, chat-binding, and migration changes:
  [Workspace topology hot reconciliation](workspace-topology-hot-reconciliation.md);
- jobs and automation files:
  [Automation hot reconciliation](automation-hot-reconciliation.md); and
- connections, routes, plugins, channel policy, server and gateway
  infrastructure, scheduler and worker settings, global queue policy, secrets,
  and repository checkout definitions: host restart.

Profile addition, deletion, or rename also remains restart-sensitive. A field
classifier cannot prove which workspaces a changed profile identity previously
affected.

## Runtime ownership inventory

`Settings.resolved_workspace_config()` expands profile includes and
`merge_workspace_profiles()` produces one `ResolvedWorkspaceConfig`. Consumers
do not share one lifetime:

| Consumer | Fields read | Lifetime and consequence |
|---|---|---|
| `host/orchestrator/_agent_runner_preflight.py` | `prompts`, `repo` | Resolves both before every turn, but a warm worker receives only messages and notices. It does not receive a replacement system prompt or mount set. |
| `host/orchestrator/agent_core_config.py` | `model` | Resolves the model before every turn. The worker core receives it only at cold start; Codex session IDs also encode the model and trigger a fresh provider session on mismatch. |
| `host/orchestrator/host_execution.py` | `execution_mode`, `cwd` | Selects host versus container execution and the host working directory before dispatch. An existing worker from the old mode must not survive the switch. |
| `host/learning/skill_activation.py` | `skills`, `denied_skills`, selected `tools` | Regenerates both agent homes before cold starts. The warm path explicitly refreshes personalized skills before each turn. Tool companion-skill metadata comes from a startup-owned map. |
| `config/tool_access.py` | selected `tools`, tool definitions, `skills`, `contains_secrets` | Filters unavailable tools from current environment requirements, adds companion skills and exposed environment, and raises the effective secret flag. |
| `host/container_manager/orchestrator.py` and `mounts.py` | `repo`, `is_admin`, learning paths, blocked mount patterns | Resolve repository worktrees and build container mounts only while spawning a worker. Admin status also adds raw host-repository mounts. |
| `host/container_manager/security/gate.py` | selected `tools`, `contains_secrets`, `cop_active`, `capabilities`, tool trust declarations | Builds one `SecurityGate` per worker invocation. The gate intentionally retains taint and session approvals for that worker lifetime. |
| `host/container_manager/mcp/manager.py` | selected `tools`, MCP definitions | Captures settings, resolved instances, workspace-to-instance assignments, proxy trust, LiteLLM teams, and virtual keys during startup sync. Static workspaces read the captured assignment directly. |
| `host/orchestrator/plugin_configuration.py` | selected `tools`, `is_admin`, `capabilities`, tool/plugin definitions | Builds plugin-owned closures and adapters for Gog, Linear, Matrix, Google setup, CalDAV, marketplace health, and other integrations at host composition. |
| `host/git_ops/repo.py` | `repo`, `repos.root`, `repos.overrides`, repository tokens | Reads current settings for path, worktree, clone, and credential resolution. The app separately captures override names, startup tokens, and externally synchronized repositories. |
| `host/learning/paths.py` and `host/orchestrator/app.py` | learning enablement, paths, review limits | Path resolution uses a replaceable runtime value, but vault mounts belong to a worker. Review switches and limits also live in app and scheduler snapshots. |
| `host/orchestrator/workspace_registration.py`, `workspace_threads.py`, and durable `WorkspaceProfile` state | `is_admin`, `contains_secrets`, `cop_active`, `capabilities` | Persist workspace metadata used by routing, scheduling, snapshots, webhooks, and child-thread creation. Startup reconciliation owns updates to this state. |

The inventory also found a stale Google setup adapter:
`GoogleSetupRuntime.workspace_is_admin` reads `settings.workspaces.*.is_admin`,
but `WorkspaceConfig` has no `is_admin` field. Resolve admin status through
`resolved_workspace_config()` before any runtime-policy implementation relies
on that adapter.

Two captured-runtime details determine the boundary:

1. `AgentExecutionRuntime` stores agent, container, mount-security, timeout, and
   model defaults once in `PynchyApp.__init__()`.
2. `agent_runner._warm_query()` sends only the new prompt text, query identity,
   metadata, and notices. It does not replace the core config, system prompt,
   mounts, environment, MCP routes, or security gate. Its sole policy refresh
   operation calls `refresh_personalized_agent_skills()`.

## Field classification

The class describes the minimum safe application mechanism. A change with
multiple field diffs takes the strongest class:

`Host restart` > `Affected-session retirement` > `Next-turn refresh`.

| Class | Contract |
|---|---|
| Next-turn refresh | All consumers resolve the field from validated current settings before the next turn. |
| Affected-session retirement | Publish the new policy, safely retire only impacted sessions, and apply it when they are recreated. |
| Host restart | Keep changes restart-sensitive because they alter host infrastructure, global concurrency, connections, plugins, workers, or another process-wide invariant. |

### Composed profile and workspace fields

| Personalized field | Class | Consumer evidence and reason |
|---|---|---|
| `profiles.*.includes` | Host restart | One include edit can change every resolved field and profile identity participates in learning paths and topology. Classify the resulting semantic field diffs in a later implementation; keep the raw composition edge restart-sensitive until that diff exists. |
| `profiles.*.skills` | Next-turn refresh | Skill activation rewrites both generated agent homes on the warm path. The selective personalization refresh already makes the resolver read current validated settings. |
| `profiles.*.denied_skills` | Next-turn refresh | Uses the same current-settings and generated-home path as grants. |
| `profiles.*.prompts` | Affected-session retirement | Preflight rereads the selection, but warm IPC cannot replace `AgentCoreConfig.system_prompt_append`. Retire the worker and provider session so one conversation never resumes under a different system prompt. |
| `profiles.*.model`, `workspaces.*.model` | Affected-session retirement | Agent-core selection rereads the effective model, but a warm core keeps its startup config. Existing Codex mismatch handling already demonstrates the required fresh-session contract. |
| `profiles.*.repo` | Affected-session retirement | Preflight rereads repository slugs, while worktree and repository mounts exist for the worker lifetime. Retire before adding, removing, reordering, or replacing mounts. |
| `profiles.*.execution_mode` | Affected-session retirement | Host/container choice runs before dispatch. Destroy the old runtime before switching execution domains. |
| `profiles.*.cwd` | Affected-session retirement | Host execution uses the resolved path for a fresh subprocess; container cores derive their working directory from cold-start repository mounts. Retire to keep one session in one execution context. |
| `profiles.*.contains_secrets` | Affected-session retirement | Tool access can strengthen this value, and the security gate snapshots it for the worker lifetime. |
| `profiles.*.cop_active` | Affected-session retirement | Cop enforcement lives in the session-scoped security gate. Replacing the gate without replacing the session would discard sticky taint and approvals. |
| `profiles.*.capabilities` | Affected-session retirement | The security gate snapshots capability rules. Matrix route validation also reads them at startup, but routes can only narrow base workspace policy; the newly created gate remains the enforcing authority. |
| `profiles.*.tools` | Host restart | This field controls far more than agent-visible tools: MCP instance/team assignment, proxy trust, companion-skill ownership, credential environment, and plugin-owned workspace adapters. No atomic MCP/plugin reconciliation owner exists. |
| `profiles.*.is_admin` | Host restart | Admin identity persists in `WorkspaceProfile` and affects routing, webhook admission, scheduler targeting, plugin routes, environment, and raw host mounts. Workspace topology reconciliation must update that durable identity before a session can safely restart. |
| `workspaces.*.profiles`, `workspaces.*.chat`, `workspaces.*.threads`, `workspaces.*.scopes`, `workspace_migrations.*` | Host restart | The topology brief owns profile assignment, registration, binding, child-thread, and retirement changes. |

### Referenced global policy

| Personalized field | Class | Consumer evidence and reason |
|---|---|---|
| `agent.model_reasoning_effort` | Affected-session retirement | The value enters `AgentExecutionRuntime` and then the cold-start core config. Publish a new execution snapshot and retire every workspace using that global setting. |
| `agent.model` | Host restart | Besides the default agent model, this value supplies plugin defaults and the Cop fallback model. Those process-wide consumers prevent a workspace-only publication. |
| `agent.default_core`, `agent.name`, `agent.trigger_aliases` | Host restart | Core resolution, gateway behavior, and the command matcher use startup-owned values. |
| `tools.*` definitions | Host restart | Tool availability, environment, trust declarations, MCP processes and proxy routes, Linear/CalDAV accounts, and plugin adapters all share these definitions. Applying only one projection could widen access or leave stale credentials. |
| `repos.root`, `repos.overrides.*` | Host restart | Repository resolution can reread settings, but Git sync, scheduler repository sets, and startup worktree token maps capture related values. |
| `learning.enabled`, `learning.obsidian.vault_root`, `learning.obsidian.mount_path`, `learning.obsidian.default_profile_root`, `learning.obsidian.memory_dir_name` | Affected-session retirement | These fields change whether and where a workspace vault mounts and where agent homes point. Publish the learning runtime, then retire every workspace whose resolved paths change. |
| `learning.review_after_turn`, `learning.max_attempts`, `learning.packet_max_chars` | Next-turn refresh | These values affect creation of future review packets or workflows, not an existing worker. Publication must replace the app and scheduler snapshots together before the next turn. |
| `security.blocked_patterns` | Affected-session retirement | Spawn-time mount validation receives a captured tuple. Publish the stricter snapshot before retiring affected workers; never leave an old worker mounted under relaxed policy. |
| `security.cop_model`, `security.cop_wire_api` | Host restart | The Cop client holds one process-wide transport selection used by all security gates. |
| `container.timeout_ms` | Next-turn refresh | Preflight chooses the query deadline for each turn. A published execution snapshot can change the next deadline without replacing the worker. |
| `container.image`, `container.memory_mb`, `container.idle_timeout_ms` | Affected-session retirement | Image, cgroup memory, and idle reclamation attach to a worker at creation. A global change affects every container session. |
| `container.runtime`, `container.max_concurrent`, `container.orphan_reap_age_ms`, `queue.*` | Host restart | These fields select process managers, global concurrency, queue retry policy, and host cleanup behavior. |

All remaining top-level settings stay restart-sensitive in this slice. They
configure host infrastructure or belong to the automation/topology briefs:
`server`, `logging`, `secrets`, `gateway`, `commands`, `scheduler`, `canary`,
`jobs`, `intervals`, `command_center`, `notifications`,
`messaging_source_health`, `connections`, `routes`, `plugins`,
`chrome_profiles`, and `user_groups`.

## Implementation contract

- Compute affected workspaces from old and candidate **resolved** policy, not
  from profile names. Includes and shared profiles can fan out to many
  workspaces.
- Reject a live publication when any changed field falls in the host-restart
  class. Do not partially apply the weaker fields from the same candidate.
- Publish one immutable current-policy snapshot before retiring sessions.
  Security, mount, and credential reductions must become visible before any
  replacement runtime starts.
- Update the affected durable `WorkspaceProfile.security` snapshot in the same
  publication transaction. Status and child-workspace consumers must not
  observe policy older than the replacement security gate.
- Retire an affected workspace only after its active turn reaches the queue
  boundary. Block new turns for that workspace during publication and
  retirement; leave unrelated workspace queues running.
- Preserve messages and conversation-control history. Clear only provider and
  worker session references needed to force cold creation.
- Roll back the published snapshot if retirement cannot complete before any
  replacement runtime starts. After replacement creation begins, fail closed
  and keep the workspace unavailable until it can use one coherent policy.

## Existing seams to reuse

- Resolved workspace configuration already centralizes profile composition.
- Session lifecycle operations already stop active work, destroy the runtime,
  clear durable session state, and mark the workspace for a fresh session.
- The selective personalization fingerprints provide a place to add another
  semantic drift class after the matrix is approved.

## Safety constraints

1. Validate the complete candidate configuration before publishing any field.
2. Apply tool, credential, mount, MCP, and security restrictions fail-closed.
3. Do not mutate policy underneath an active turn.
4. Do not clear conversation history merely to replace a runtime session.
5. Keep process-wide settings restart-sensitive unless a specific live owner
   and atomic update contract exist.
6. Preserve unrelated sessions and queues.

## Acceptance criteria

- [x] Inventory every consumer of each candidate field, including captured
  runtime dataclasses and plugin-provided adapters.
- [x] Approve the field-by-field classification matrix.
- [x] Pause affected queues at a turn boundary before publication and
  retirement.
- [x] Publish durable workspace security and current runtime snapshots before
  replacing sessions; roll back before resuming queues when retirement fails.
- [x] Prove affected-session replacement, sticky-taint preservation, queued
  work preservation, and unaffected-workspace continuity.
