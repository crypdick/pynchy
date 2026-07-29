# Automation Hot Reconciliation

**Status:** Implemented.

**Outcome:** Apply valid changes to personalized automation definitions without
restarting Pynchy. Invalid configuration keeps the last published runtime
snapshot and schedules.

## Why this is separate

Automation changes affect three durable runtimes with different behavior:

- Configured agent jobs are persisted as scheduled tasks.
- Configured host cron jobs are read by Temporal activities.
- Temporal schedules must be created, updated, paused, or deleted.

Treating this as a settings-cache reset would leave at least one runtime using
stale state.

## Implementation

- `automation_projection()` fingerprints file-backed automation definitions and
  referenced prompt content independently from restart-sensitive settings.
- `reconcile_agent_jobs()` creates, updates, pauses, resumes, and
  rebinds configured agent jobs.
- `reconcile_temporal_schedules()` reconciles database tasks, host jobs,
  configured cron schedules, and stale Temporal schedules.
- `PynchyApp.apply_config_candidate()` waits for startup recovery, builds the
  candidate scheduler snapshot, runs both reconciliation use cases, and
  publishes the snapshot only after they succeed.

Configured host cron activities read the replaceable
`PynchyApp.scheduler_runtime` snapshot rather than a startup-only mapping.

## Runtime contract

1. Classify automation drift separately from restart-sensitive settings.
2. Parse and validate the complete candidate before changing
   durable or in-memory state.
3. Reconcile configured tasks and Temporal schedules before publishing one
   coherent scheduler snapshot.
4. Cover additions, updates, enable or disable transitions, removals, workspace
   rebinding, and prompt-bundle changes.
5. Keep the previous published snapshot on validation or reconciliation
   failure.
6. Retry reconciliation on the next host-sync poll. Temporal cannot transact a
   multi-schedule update, so an interrupted pass may leave partial durable
   mutations; the existing idempotent reconcilers converge them on retry.

## Non-goals

- Workspace registration or thread topology.
- Model, tool, security, repository, or container runtime policy.
- Changes to Temporal connection, namespace, task queue, or worker lifecycle.

## Acceptance criteria

- [x] Automation definitions and referenced prompts have a field-level
  projection independent from the restart fingerprint.
- [x] Reconciliation waits for startup recovery.
- [x] Configured tasks and Temporal schedules reconcile before publication.
- [x] Validation or reconciliation failure leaves the published runtime
  snapshot unchanged.
- [x] Add, update, disable, remove, workspace-rebind, and prompt-change paths
  use idempotent reconciliation.
