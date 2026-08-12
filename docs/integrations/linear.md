# Linear

The built-in Linear integration gives each Pynchy workspace a durable work
board. Linear records proposals, human authorization, active ownership, and
results. The agent decides how to plan and execute authorized work.

## Configure access

Create a Linear personal API key and store it in the host environment. The
single-account form reads `LINEAR_API_KEY` and, when needed,
`LINEAR_TEAM_KEY`:

```bash
LINEAR_API_KEY=lin_api_...
LINEAR_TEAM_KEY=SYN
```

```toml
[tools.linear]
type = "linear"
skills = ["linear"]
required_env = ["LINEAR_API_KEY"]
optional_env = ["LINEAR_TEAM_KEY"]
expose_env_to_workspace = true
project_per_workspace = true

[profiles.project]
tools = ["linear"]

[workspaces.code-improver]
profiles = ["project"]
```

The Linear companion skill calls the provider directly, so this tool uses the
exceptional `expose_env_to_workspace = true` setting. Selecting `linear`
installs the skill and exposes only its declared variables to the workspace.
See [Tool access and secrets](../usage/tool-access.md) for the authorization
model.

For multiple accounts, declare one tool per API key. The tool declaration is
the account and data-policy boundary:

```bash
LINEAR_SYNAPSE_API_KEY=lin_api_...
LINEAR_SYNAPSE_TEAM_KEY=SYN
```

```toml
[tools.linear_synapse]
type = "linear"
required_env = ["LINEAR_SYNAPSE_API_KEY"]
optional_env = ["LINEAR_SYNAPSE_TEAM_KEY"]
project_per_workspace = true
public_source = false
secret_data = false
public_sink = true
dangerous_writes = false

[profiles.project]
tools = ["linear_synapse"]
```

A workspace can select at most one Linear account. Pynchy maps that account's
declared variables to Linear's standard runtime names for its managed process
and uses the configured names for host-side work-item state. This multi-account
form keeps custom credential names out of the agent workspace.

Changing the materialized host environment requires the normal managed Pynchy
restart. Local `.env` edits trigger that flow automatically. Restart manually
only if the health check shows that the service is stuck.

## Use workspace boards

At startup, Pynchy creates missing workspace projects and workflow states and
reconciles the positions of its managed states. This keeps the visible order
`Agent Proposed` → `Ready for Planning` → `Awaiting Plan Approval` → `Human
Approved` → `In Progress` even when states predate the current workflow. Each
project carries an immutable workspace marker. Runtime reads and writes require
exactly one marked project and fail closed if the project is missing or
ambiguous.

Reconciliation doesn't delete or archive existing Linear objects, change state
types, or reorder states it doesn't manage.

| State | Meaning |
|-------|---------|
| `Agent Proposed` | The agent suggested work and awaits human review. No human authorized it; the need for review does not make it blocked. |
| `Ready for Planning` | A human requested a concrete plan. Execution remains unauthorized. |
| `Awaiting Plan Approval` | The agent persisted a plan for human review. |
| `Human Approved` | A human explicitly authorized execution. |
| `In Progress` | The host owns an active execution lease. |
| `Awaiting Review` | The delivered result awaits review. For code, every PR is attached to the issue. |
| `Follow-ups` | The main result is ready; the agent is finishing operational loose ends. |
| `Blocked` | A concrete external dependency prevents authorized work from proceeding. |
| `Done` | The agent judges the complete job, including follow-ups, finished. |
| `Rejected` | A human declined the proposal. |

Planning authorization, execution authorization, and active ownership form
separate host-enforced boundaries. The agent chooses how to investigate, plan,
implement, validate, review, follow up, and complete work within the authority
granted by the current state.

### Plan and authorize work

Move an item to `Ready for Planning` when you want a concrete implementation
plan without authorizing execution. Temporal admits a planning turn in the
issue's durable routed conversation. The agent calls `linear_submit_plan` to
persist the plan in the issue description and move the item to `Awaiting Plan
Approval`.

While the item is `Awaiting Plan Approval`, comments can ask the agent to
revisit assumptions or refine the plan. The agent can call
`linear_submit_plan` again to atomically replace the marked plan section
without moving the issue backward or granting execution authority. The item
remains `Awaiting Plan Approval` for human review.

Review the plan, then move the issue to `Human Approved` in Linear. You can also
explicitly request that state change in a direct human conversation. The agent
can then use `linear_move_todo`; Pynchy verifies that the current turn came
directly from a human without requiring matching wording. Moving the issue
directly to `In Progress` in Linear is also an approval action when an
authenticated callback route is configured: when the signed webhook identifies
a user actor and a state change, Pynchy acquires the lease in place instead of
moving the issue backward first.

Before Pynchy leases a `Human Approved` item that contains a marked plan, a
hidden agent with provider tools disabled checks that plan against the current
repositories. The reviewer is instructed to inspect without modifying them. The
reviewer uses implementation judgment: ordinary drift can proceed, while a
minor inaccuracy can amend the canonical plan and continue from `Human Approved`
without another approval cycle. The execution worker receives that provider-
confirmed amended revision. Drift that requires a major product or technical
decision returns a complete replacement plan to `Awaiting Plan Approval` with an
explanatory comment. A reviewer error also adds a comment and returns the issue
to `Awaiting Plan Approval`. Neither material replanning nor reviewer failure
acquires a lease. The issue's Discord thread posts when this check starts and
whether it admitted work. Items approved without a marked plan skip this review.

`linear_create_todo` creates an unapproved `Agent Proposed` item. An autonomous
agent can't set `Ready for Planning`, `Human Approved`, or `Rejected`.

### Execute and report

Before admitting work from `Human Approved`, the host acquires a durable,
idempotent lease and moves the issue to `In Progress`. Only one active Pynchy
execution can own an issue. Planning, execution, retries, follow-ups, recovery,
comments, and progress questions all use the same issue thread runtime and
worktree. Workflow transitions don't reset its provider session.

The agent receives the objective, authority, and success condition, then
chooses how to investigate, plan, execute, validate, publish, and report. It can
add evidence or context with `linear_create_comment`, attach each pull request
with `linear_create_attachment`, and use `linear_move_todo` to keep the issue's
state accurate. Returning a final response doesn't imply a state transition.

`Awaiting Review`, `Follow-ups`, `Blocked`, and `Done` are agent-managed
outcomes. The agent exercises judgment rather than satisfying a harness rule.
The generic `linear_move_todo` action accepts a typed `outcome` object for
these linked transitions. `Awaiting Review`, `Follow-ups`, and `Done` require a
nonempty `summary`; `Blocked` requires a nonempty `blocker` and defaults its
summary to that blocker. The action can also record `handoff_to` and
`evidence_refs`. Omitted outcome fields preserve evidence already stored on the
execution.

The execution's `turn_id` remains immutable provenance for the turn that first
owned the lease. Each reporting transition separately records the current
requester-delivery turn. A later resumed or follow-up turn replaces that
delivery correlation atomically. Only successful delivery of the visible final
for the matching turn marks the outcome delivered; a different turn or failed
channel send leaves it pending.

`Follow-ups` covers work such as deployment verification, preserving useful
logs before teardown, cleaning feature resources, and updating or unblocking
related issues. The agent can move the issue to `Done` when the whole job is
genuinely finished.

`Human Approved` and `Rejected` record human decisions. A direct human can ask
the agent to make those ordinary state changes; the host checks the turn's
provenance, not magic wording. A later move from `Blocked`, `Done`, or
`Rejected` to `Human Approved` can start a new execution attempt.

Resetting context during active execution is an explicit cancellation. Pynchy
cancels the task and Temporal attempt, retires the checkpoint, preserves
worktree changes, records the execution as `CANCELLED`, moves the issue to
`Blocked`, and posts an explanation. The reset clears the provider session and
posts `🗑️`. Automatic recovery remains disabled until a human moves the issue
from `Blocked` to `Human Approved`.

`In Progress` remains lease-managed, so neither the human nor agent sets it
through the generic tool. A direct Linear UI move is handled only through the
authenticated user-transition path above.

If the provider result is uncertain after a network failure, use
`linear_reconcile_work_item` before retrying a mutation. After reviewing a
conflict, use it only when Linear already has the intended state; it reads
Linear and never retries the provider write.

Pynchy-owned Linear comments carry an invisible request marker. When their
create response is lost, replaying the same tool request reads a bounded set of
issue comments and confirms the original action only if one exact marker/body
match exists. It never sends a second comment. Comments created before this
marker was introduced, or reads with zero or multiple matches, remain
`OUTCOME_UNKNOWN` and require human follow-up rather than a retry.

## Receive Linear callbacks

Configure one route for all managed boards:

```toml
[[plugins.linear.options.webhook_routes]]
name = "managed-boards"
secret_env = "LINEAR_WEBHOOK_SECRET"  # pragma: allowlist secret
organization_id = "your-linear-organization-id"
```

Use `workspace` only for a deliberately fixed single-board route:

```toml
[[plugins.linear.options.webhook_routes]]
name = "code-improver"
workspace = "code-improver"
tool = "linear_synapse"
secret_env = "LINEAR_WEBHOOK_SECRET"  # pragma: allowlist secret
organization_id = "your-linear-organization-id"
```

Store the Linear signing secret in the host environment:

```bash
LINEAR_WEBHOOK_SECRET=...
```

Expose Pynchy through the
[control-plane public-bind setup](../usage/control-plane.md#enable-remote-diagnostic-access),
then subscribe Linear `Comment` and `Issue` events to:

```text
https://pynchy.example.com/webhooks/linear/<route-name>
```

Linear requires a public HTTPS URL. Pynchy doesn't provide TLS or a hostname.
See [Linear's webhook documentation](https://linear.app/developers/webhooks).

Pynchy maps the issue's marked project to its workspace and uses the immutable
issue ID for conversation identity. Deliveries for off-board issues are
ignored. Pynchy ignores an `Issue/create` callback only when its signed callback
state matches the managed `Agent Proposed` state, because creating a proposal
does not authorize work. Other issue creations follow the normal callback
policy. Callback state stays immutable for that delivery, so a later state
transition arrives as a separate `Issue/update` callback. A project-assignment
update never wakes an agent when its changed fields contain `projectId` plus
only Linear's `addedToProjectAt` and `updatedAt` bookkeeping. Pynchy still
resolves the issue's workspace ownership before it records the delivery as
ignored. An update that also changes a substantive field follows the normal
callback policy. Ordinary comments, other nonterminal issue updates and
removals, and messages in the corresponding Discord thread share one ordered
conversation.

A `Ready for Planning` update belongs to the planning controller. A `Human
Approved` update also remains controller-owned and immediately triggers
work-item reconciliation. The controller starts an issue-revision-specific
Temporal plan review before it acquires the execution lease. A user-authored
state transition directly to `In Progress` acquires the same lease without
another provider move. An unleased `In Progress` update without that actor and
changed-field evidence remains controller-owned but does not authorize work.
These issue updates don't also start ordinary conversation turns, because that
would race a second agent against the durable task. An `Awaiting Plan Approval`
update waits for human review.

An `Issue` callback with Linear workflow type `completed` or `canceled` becomes
a terminal lifecycle delivery. Pynchy uses the type, not a mutable display name,
to recognize terminal state. At ingress, it records terminal conversation intent,
clears the routed session, retires prior routed work, and archives an existing
Discord thread. It never starts an LLM turn. A terminal callback that arrives
first records the same intent but creates no Discord thread.

While Linear remains terminal, comments, stale callbacks, and delayed scheduled
work cannot recreate or unarchive that thread. An explicit later nonterminal
Linear state reopens normal conversation handling. Pynchy completes the linked
reviewed execution only when the callback's exact state ID matches the managed
board's `Done` state. `Duplicate`, `Canceled`, and other terminal statuses cancel
the local scheduled task and active execution without writing a new provider
state; they do not complete the work item. Archive or lifecycle processing
failure stays retryable. Pynchy captures both state IDs during ingress, so later
issue deletion or project movement cannot change the terminal decision.

Before a host-owned comment creation or nonterminal state move starts, Pynchy
records a durable outbound-effect intent. If its callback arrives before the API
response, Pynchy acknowledges and holds it at the issue's FIFO head. Exact
response evidence completes the self callback without an agent turn; mismatched
evidence releases the callback in place. Confirmed evidence is retained so
provider retries and every configured route make the same decision. Comments
from someone sharing the same Linear account remain ordinary routed input once
their exact evidence does not match. Terminal state callbacks never use this
suppression path: Pynchy still processes them as lifecycle-only control work.
The receipt, parsed delivery envelope, effect candidates, and initial FIFO state
commit together. Trusted Linear processing runs before admission only for
callbacks already known to be unrelated; held candidates run it from the
persisted envelope after release.

A host stop or transport failure after external I/O begins can leave the
provider outcome unknowable. Pynchy quarantines matching callbacks as
`outcome_unknown` rather than risking a self wake. It releases only effects
known not to have reached the provider. This fail-closed state requires
reconciliation if it occurs. `GET /webhook-effects` lists quarantined effects
and their request-field hashes. After checking Linear independently and proving
the mutation absent, an authenticated operator can post
`{"verified_absent": true}` to
`/webhook-effects/{effect_id}/reconcile-absent` to release its held callbacks.
See [Routed conversations](../architecture/conversation-routing.md#linear-issue-webhooks)
for the callback identity and recovery contract.

The route verifies Linear's HMAC-SHA256 signature, requires a timestamp within
60 seconds, checks the configured organization ID, and deduplicates delivery
UUIDs. The signing secret never enters the agent container. The selected Linear
account's `public_source` setting determines whether callback content is
trusted.

One Temporal schedule reconciles every managed board once per minute as a
recovery backstop, including webhook-routed boards. It creates or recovers
planning tasks for `Ready for Planning`, starts one idempotent plan-review
workflow for each marked `Human Approved` issue revision, repairs an `In
Progress` execution whose durable task is missing, and admits `Follow-ups`
without a second approval lease. Each plan review runs independently, so a slow
review doesn't block discovery or review of another approved issue. `Awaiting
Plan Approval` remains idle until a human decides.

Before admitting work, the same reconciliation compares every unfinished local
execution with its current Linear state and settles interrupted provider
transitions after the live request grace period. A missed move to `Done` or
another typed terminal state closes the routed conversation and retires its
session, task, workflow, and in-flight turn. A nonterminal state that no longer
authorizes the execution cancels only that execution's work, preserving the
conversation and provider session for a later authorized attempt.
Both paths retire ephemeral workspace artifacts. A clean worktree checkout gets
removed while its branch remains available for a later reopen. Dirty or untracked
work or user workspace files block cleanup so unfinished work stays recoverable. See
[Worktree isolation](../usage/worktrees.md#worktree-isolation).

Only an explicit work-item lifecycle outcome, such as `Awaiting Review`,
`Blocked`, `Follow-ups`, `Done`, or cancellation, counts as a successful Linear
execution occurrence. A clean agent exit without one records `incomplete` and
completes the schedule occurrence without an immediate Temporal retry. After a
five-minute grace period, reconciliation may reactivate the task; successful and
incomplete occurrences share a three-occurrence cap. Failed activities retain
their checkpoint and use Temporal's normal retry path. An `In Progress` issue
without a matching lease produces an invariant violation and never becomes
implicit authorization. A completed planning task tied to the exact durable
issue ID can also supply approval evidence for an execution that started before
execution leases existed.

Because the schedule lives in Temporal, a deploy or transient worker outage
doesn't erase the recovery intent. Once the worker is healthy, the next
one-minute reconciliation recreates missing work; an interrupted agent turn
resumes from its durable checkpoint and provider session. The routed issue
conversation is the sole owner of planning, execution, and interactive work.

## Schedule proactive proposals

Use a config-backed [agent task](../usage/scheduled-tasks.md#agent-tasks) for a
bounded review. Its prompt should state the review objective, proposal-only
authority, and the quality bar for useful proposals. Let the agent choose its
investigation method and how many worthwhile items to create.

`linear_create_todo` keeps every scheduled proposal in `Agent Proposed`, so the
review can't authorize or execute its own suggestions.

## Use Linear tools

The Linear MCP server provides ordinary provider tools:

| Tool | Purpose |
|------|---------|
| `linear_list_teams` | Lists teams visible to the selected account. |
| `linear_list_issues` | Lists recent issues, optionally by team. |
| `linear_search_issues` | Finds issues by case-insensitive title text, optionally by team. |
| `linear_get_issue` | Gets an issue by stable Linear ID. |
| `linear_create_issue` | Creates an ordinary issue without an approval-bearing state; optionally sets priority (`0` none, `1` urgent, `2` high, `3` medium, `4` low). |
| `linear_list_todos` | Lists open items on the current workspace board. |
| `linear_create_todo` | Creates an unapproved workspace proposal. |
| `linear_create_attachment` | Attaches an external URL, including every pull request produced by the work, to an issue. |
| `linear_find_issues_by_attachment_url` | Resolves an exact external URL, such as a PR URL from a GitHub event, back to attached issues. |

Pynchy's built-in agent tools expose host-managed comment, state, and lease
actions:

| Tool | Purpose |
|------|---------|
| `linear_create_comment` | Adds a workspace-owned comment to an issue without reopening its own conversation. |
| `linear_submit_plan` | Atomically persists an initial or revised plan and leaves it `Awaiting Plan Approval`. |
| `linear_reconcile_work_item` | Resolves an uncertain transition or reviewed conflict already at its intended state. |
| `linear_list_work_items` | Lists durable execution records for the workspace. |
| `linear_move_todo` | Moves work to an agent-managed outcome with typed evidence, or to a directly human-authorized state. |

## Inspect executions

Operators can inspect the read-only work-item projection through:

```text
GET /work-items?workspace=<workspace>&limit=100
```

The response includes the issue, workspace, immutable owner turn and task
links, lifecycle state, evidence, requester-delivery turn and outcome, and
timestamps. It doesn't fetch issue descriptions.

Linear is a public sink by default because issue creation sends data outside
Pynchy. A deployment that trusts its private Linear workspace and every member
can set `public_sink = false`. Workspace policy can still require approval
before an agent uses the tool.

To extend work-item behavior beyond the built-in integration, see
[Plugin authoring](../plugins/index.md).
