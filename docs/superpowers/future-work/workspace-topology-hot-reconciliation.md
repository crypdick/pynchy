# Workspace Topology Hot Reconciliation

**Status:** Future-work brief; not implementation-ready.

**Outcome:** Apply valid workspace, thread, profile-assignment, durable admin
identity, and migration changes without restarting unrelated workspaces.

## Why this is separate

Workspace topology has external and durable side effects. A change can create
or retire channel resources, change JID bindings, alter child-thread routing,
and pause jobs whose target no longer exists. Those operations need explicit
ordering and recovery rather than a generic settings refresh.

## Existing seams to reuse

`reconcile_workspaces()` already provides the idempotent startup use case. It:

- registers missing workspaces and removes eligible orphans;
- synchronizes resolved workspace profiles, including durable admin identity
  and security metadata;
- reconciles child threads;
- reconciles configured agent jobs; and
- protects legacy workspaces that still own non-terminal tasks.

The future implementation should call this use case through a composition-root
port instead of reproducing its individual steps in the Git-sync subsystem.

## Required behavior

1. Classify workspace-topology drift independently from runtime policy drift.
2. Validate the complete desired topology before creating or removing anything.
3. Publish changed durable admin identity before starting a replacement
   runtime.
4. Preserve stable JIDs and existing conversations when their logical workspace
   still exists.
5. Order additions, rebindings, job updates, migrations, and removals so no
   scheduled work targets a retired runtime.
6. Retire removed workspace sessions and queued work through existing lifecycle
   operations.
7. Treat unsupported channel operations as a failed reconciliation, not as
   silent partial success.
8. Make retries idempotent after partial external-channel failure.

## Non-goals

- Reconfiguring channel connections or plugin enablement.
- Applying model, tool, non-identity security, repository, or container policy
  to retained workspaces.
- Automating destructive conversation-history deletion.

## Entry criteria for an implementation plan

- Define the ownership boundary between the drift detector,
  `reconcile_workspaces()`, and channel adapters.
- Specify per-channel create, rebind, and remove guarantees.
- Define the exact ordering and rollback rules for migrations and removals.
- Define how durable admin identity publication composes with affected-session
  retirement.
- Decide how active turns, pending messages, and scheduled tasks block or defer
  retirement.
- Define acceptance scenarios for add, profile reassignment, thread change,
  migration, and remove.

Until these decisions are documented, workspace-topology changes remain
restart-sensitive.
