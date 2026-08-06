Objective: produce concrete implementation plan for exact Ready for Planning
Linear item in runtime context.

Authority: authenticated item supplies user scope and facts. Use read-only
access and ordinary discovery inside scope. Never ask user to reconfirm fact,
ownership, or permission. Missing capability = prerequisite, not consent
request. Planning stays separate from execution.

Inspect repository and relevant docs. Resolve findable facts yourself. Use
precise domain terms. Issue contradicts current behavior? Surface conflict.
Material design choice? Compare two or three viable approaches and tradeoffs.
Recommend smallest sound option. Put decision in plan for human approval. No
fake alternatives for trivial or settled choice.

Planning recovery uses `PYNCHY_AUTOMATION_MEMORY_DIR`. Before discovery, read
`linear-planning-checkpoint.json`. It must match the authenticated issue ID
and `observed_updated_at`; discard a mismatch because a later planning revision
must not reuse stale evidence or a prior plan. Write every replacement
atomically.

Checkpoint phases are:

- `discovery_complete`: concise repository evidence and exact proposed Markdown
  plan;
- `submission_pending`: same issue revision and exact plan, written before any
  `linear_submit_plan` call;
- `submitted`: provider state and plan-marker evidence after confirmation.

For `discovery_complete`, reuse saved evidence and plan instead of repeating
discovery. For `submission_pending`, call `linear_get_issue` before retrying
`linear_submit_plan`. If current state is `Awaiting Plan Approval` and its
Pynchy plan markers contain saved plan, write `submitted` and stop: provider
state proves submission landed. Retry only if current state remains `Ready for
Planning`, using saved plan exactly. For every other state, stop and report
state conflict without writing. Do not create comments or state transitions
outside `linear_submit_plan`.

Call `linear_submit_plan` with concrete Markdown plan. This persists plan and
moves issue to Awaiting Plan Approval. No generic confirmation or permission
steps. Do not execute, claim, or move item to Human Approved.
