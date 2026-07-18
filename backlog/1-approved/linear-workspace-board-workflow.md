# Make Linear workspace boards drive Pynchy work

## Goal

Turn an opted-in workspace's Linear project from a create-and-forget todo
mirror into its visible, durable planning and work-lifecycle surface.

## Scope

- Treat the selected workspace project as the canonical todo source when the
  Linear tool is enabled; preserve the local todo path only for workspaces that
  do not opt in.
- Provide a bounded current-board snapshot to an interactive or scheduled run
  and a typed way to select one workspace issue as the work item for that run.
  The automatic snapshot includes only safe issue identity, title, and status;
  longer issue content requires an explicit read.
- When a run claims a selected issue, move it to In Progress and persist the
  Linear issue id with Pynchy's run or task record. Refresh that association on
  resume and surface an external status change rather than overwriting it.
- Provide explicit complete and block/handoff operations that update the linked
  issue with a concise result and the matching Linear state. Do not infer Done
  from a conversational response or a process exit.
- Make `todo ...`, task listing, scheduled work, and agent tools converge on
  this one workspace-board contract. Add hermetic lifecycle coverage and extend
  the existing Linear canary with the run-to-issue association.

## Boundary

Linear remains the human-facing source of truth for planned work. Pynchy owns
the runtime execution record and must link to Linear rather than build a second
Kanban product. This does not autonomously dispatch every Ready issue, assign
people, or replace the planned managed-flow/delegation model.
