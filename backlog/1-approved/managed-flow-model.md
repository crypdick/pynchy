# Managed flow model on Temporal

## Goal

Build a product-level managed-flow model on top of Temporal for durable,
operator-visible work that can wait, resume, cancel, and create child flows.

## Scope

- Model owner/requester identity, goal, current step, status, and typed state.
- Define typed wait states, explicit resume and cancellation signals, and
  revisioned transitions to prevent stale updates.
- Support parent/child flow relationships and operator-facing flow status.
- Map the model onto Temporal workflows, timers, retries, and durable history.

## Boundary

Temporal remains the durable orchestration engine. This work adds the missing
Pynchy product contract for human-wait and resumable work; it does not replace
Temporal with an application-owned scheduler or untyped JSON state machine.
