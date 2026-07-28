# Automation Hot Reconciliation

**Status:** Future-work brief; not implementation-ready.

**Outcome:** Apply valid changes to personalized automation definitions without
restarting Pynchy, while preserving the last valid schedules when new
configuration is invalid.

## Why this is separate

Automation changes affect three durable runtimes with different behavior:

- Configured agent jobs are persisted as scheduled tasks.
- Configured host cron jobs are read by Temporal activities.
- Temporal schedules must be created, updated, paused, or deleted.

Treating this as a settings-cache reset would leave at least one runtime using
stale state.

## Existing seams to reuse

- `reconcile_agent_jobs()` already creates, updates, pauses, resumes, and
  rebinds configured agent jobs.
- `reconcile_temporal_schedules()` already reconciles database tasks, host jobs,
  configured cron schedules, and stale Temporal schedules.
- `PynchyApp.scheduler_runtime` already provides the scheduler-owned runtime
  contract.

The missing seam is a safe replacement for
`SchedulerRuntimeConfig.config_host_cron_jobs`, which is built once at startup
and read when each configured host cron activity runs.

## Required behavior

1. Classify automation-only drift separately from restart-sensitive settings.
2. Parse and validate the complete proposed configuration before changing
   durable or in-memory state.
3. Publish one coherent scheduler snapshot, then run the existing
   reconciliation use cases.
4. Cover additions, updates, enable or disable transitions, removals, workspace
   rebinding, and prompt-bundle changes.
5. Keep the previous snapshot and schedules on validation or reconciliation
   failure, and report the failure to the operator.
6. Make retries idempotent and safe after a partial Temporal failure.

## Non-goals

- Workspace registration or thread topology.
- Model, tool, security, repository, or container runtime policy.
- Changes to Temporal connection, namespace, task queue, or worker lifecycle.

## Entry criteria for an implementation plan

- Define the application-owned operation that atomically publishes the new
  scheduler snapshot and invokes reconciliation.
- Decide rollback behavior when publication succeeds but a Temporal mutation
  fails.
- Prove how running workflows and one-shot jobs behave when their definition is
  updated or removed.
- Specify a field-level fingerprint for automation definitions and fixtures.
- Define an end-to-end acceptance check for add, update, disable, and remove.

Until these decisions are documented, automation changes remain
restart-sensitive.
