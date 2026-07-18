# Make Linear workspace boards drive Pynchy work

## Goal

Turn an opted-in workspace's Linear project from a create-and-forget todo
mirror into its visible, durable planning and work-lifecycle surface.

Keep a Linear work item, a Pynchy execution, and a managed flow distinct:

- A **Linear work item** represents planned work for people.
- A **Pynchy execution** represents one attempt to do that work, including one
  firing of a recurring scheduled task.
- A **managed flow** represents multi-step or delegated work that can wait,
  resume, cancel, and create children.

One work item can have multiple executions. A recurring schedule does not
permanently own one work item. A managed flow owns and links its executions;
the separate managed-flow plan owns its lifecycle semantics.

## Scope

- Treat the selected workspace project as the canonical todo source when the
  Linear tool is enabled; preserve the local todo path only for workspaces that
  do not opt in.
- Use the workspace board states `Backlog`, `Planning`, `Ready`, `In Progress`,
  `Blocked`, and `Done`. Do not map a blocked execution to `Planning`; doing so
  hides an active operational condition from the human-facing board.
- Provide a bounded current-board snapshot to an interactive or scheduled run
  and a typed way to select one workspace issue as the work item for that run.
  The automatic snapshot includes only safe issue identity, title, and status;
  longer issue content requires an explicit read and remains untrusted external
  data rather than an instruction from the requester.
- When a run claims a selected issue, move it to In Progress and persist the
  Linear issue id with a Pynchy execution record, not with a schedule
  definition. The claim records the Pynchy execution or Temporal workflow ID,
  initiating identity, observed Linear state/version, and attempt number.
  Resume reuses that association rather than creating a second claim.
- Make claiming conflict-aware. Serialize local active claims for one Linear
  issue, read the latest remote state before a transition, use a
  provider-supported revision check when available, and never blindly retry a
  conflicting remote transition. Surface a human or another agent moving the
  issue as a conflict instead of overwriting it.
- Provide explicit complete and block/handoff operations that update the linked
  issue with a concise result, evidence references, and the matching Linear
  state. Use `claim_work_item`, `complete_work_item`, `block_work_item`, and
  `handoff_work_item` for a linked execution. Keep the generic todo move tool
  for unlinked planning work only. Do not infer Done from a conversational
  response or a process exit.
- Keep execution, Linear mutation, and requester delivery outcomes separate.
  Persist an intended Linear transition and its receipt or unknown outcome so a
  crash after a remote write does not duplicate it. A successful execution with
  failed Linear mutation or failed requester delivery remains visible for
  reconciliation.
- Let each scheduled firing either run unlinked, claim one explicitly selected
  Ready issue, or resume its existing linked execution. Do not dispatch every
  Ready issue automatically.
- Make `todo ...`, task listing, scheduled work, and agent tools converge on
  this one workspace-board contract. Provide a read-only work-item view with
  its Linear URL, linked execution or flow, local state, latest attempt,
  blocker, and last known remote state. Build this view from authoritative
  records rather than a dashboard-only state layer.
- Add hermetic coverage for claim races, resume, remote-state conflicts,
  `Blocked` and `Done` transitions, retry/reconciliation after each outcome,
  workspace isolation, and the rule that a scheduled firing never sweeps Ready
  work. Extend the existing Linear canary with a run-to-issue association and
  its cleanup.

## Boundary

Linear remains the human-facing source of truth for planned work. Pynchy owns
the runtime execution record and must link to Linear rather than build a second
Kanban product. This does not autonomously dispatch every Ready issue, assign
people, or replace the planned managed-flow/delegation model. The managed-flow
plan owns parent/child execution, typed waits, cancellation, and flow-level
status; this plan only defines how a flow or one-off execution links to a
human-facing work item.
