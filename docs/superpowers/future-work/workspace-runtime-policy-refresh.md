# Affected-Workspace Runtime Policy Refresh

**Status:** Discovery brief; not implementation-ready.

**Outcome:** Refresh supported workspace policy without restarting the host,
retiring only affected sessions when a fresh runtime is required.

## Why this is separate

Resetting the settings cache does not update every consumer. Pynchy also builds
startup-owned runtime values for agent execution, queue policy, mounts,
security, tools, MCP, repositories, learning, and scheduler behavior. A generic
"settings hot reload" would therefore be incomplete and could apply sensitive
policy inconsistently.

## Required classification

Create an evidence-backed matrix for each personalized field:

| Class | Contract |
|---|---|
| Next-turn refresh | All consumers resolve the field from validated current settings before the next turn. |
| Affected-session retirement | Publish the new policy, safely retire only impacted sessions, and apply it when they are recreated. |
| Host restart | Keep changes restart-sensitive because they alter host infrastructure, global concurrency, connections, plugins, workers, or another process-wide invariant. |

Candidate fields include model and reasoning effort, prompt selection, tool
access, repository mounts, execution mode and working directory, MCP
availability, learning paths, and security policy. Classification must follow
actual consumers rather than field names.

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

## Entry criteria for an implementation plan

- Inventory every consumer of each candidate field, including captured runtime
  dataclasses and plugin-provided adapters.
- Approve the field-by-field classification matrix.
- Define the safe point and lifecycle operation for affected-session
  retirement.
- Define atomic publication and rollback behavior for security-sensitive
  policy.
- Define acceptance checks proving both affected-workspace refresh and
  unaffected-workspace continuity.

Until the matrix and lifecycle contract are approved, these fields remain
restart-sensitive.
