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
project_per_workspace = true

[profiles.project]
tools = ["linear"]

[workspaces.code-improver]
profiles = ["project"]
```

For multiple accounts, declare one tool per API key. The tool declaration is
the account and data-policy boundary:

```bash
LINEAR_SYNAPSE_API_KEY=lin_api_...
LINEAR_SYNAPSE_TEAM_KEY=SYN
```

```toml
[tools.linear_synapse]
type = "linear"
api_key_env = "LINEAR_SYNAPSE_API_KEY"  # pragma: allowlist secret
team_key_env = "LINEAR_SYNAPSE_TEAM_KEY"
public_source = false
secret_data = false
public_sink = true
dangerous_writes = false

[profiles.project]
tools = ["linear_synapse"]
```

A workspace can select at most one Linear account. Pynchy maps that account's
key into the workspace's Linear MCP process and uses the same account for
host-side work-item state.

Editing `.env` triggers the normal Pynchy restart. Restart manually only if the
health check shows that the service is stuck.

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
| `Agent Proposed` | The agent suggested work. No human authorized it. |
| `Ready for Planning` | A human requested a concrete plan. Execution remains unauthorized. |
| `Awaiting Plan Approval` | The agent persisted a plan for human review. |
| `Human Approved` | A human explicitly authorized execution. |
| `In Progress` | The host owns an active execution lease. |
| `Awaiting Review` | The delivered result awaits review. For code, every PR is attached to the issue. |
| `Follow-ups` | The main result is ready; the agent is finishing operational loose ends. |
| `Blocked` | The result needs intervention or another owner. |
| `Done` | The agent judges the complete job, including follow-ups, finished. |
| `Rejected` | A human declined the proposal. |

Planning authorization, execution authorization, and active ownership form
separate host-enforced boundaries. The agent chooses how to investigate, plan,
implement, validate, review, follow up, and complete work within the authority
granted by the current state.

### Plan and authorize work

Move an item to `Ready for Planning` when you want a concrete implementation
plan without authorizing execution. Temporal admits one durable isolated
planning task. The agent calls `linear_submit_plan` to persist the plan in the
issue description and move the item to `Awaiting Plan Approval`.

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

`linear_create_todo` creates an unapproved `Agent Proposed` item. An autonomous
agent can't set `Ready for Planning`, `Human Approved`, or `Rejected`.

### Execute and report

Before admitting work from `Human Approved`, the host acquires a durable,
idempotent lease and moves the issue to `In Progress`. Only one active Pynchy
execution can own an issue.

The agent receives the objective, authority, and success condition, then
chooses how to investigate, plan, execute, validate, publish, and report. It can
add evidence or context with `linear_create_comment`, attach each pull request
with `linear_create_attachment`, and use `linear_move_todo` to keep the issue's
state accurate. Returning a final response doesn't imply a state transition.

`Awaiting Review`, `Follow-ups`, `Blocked`, and `Done` are agent-managed
outcomes. The agent exercises judgment rather than satisfying a harness rule.
`Follow-ups` covers work such as deployment verification, preserving useful
logs before teardown, cleaning feature resources, and updating or unblocking
related issues. The agent can move the issue to `Done` when the whole job is
genuinely finished.

`Human Approved` and `Rejected` record human decisions. A direct human can ask
the agent to make those ordinary state changes; the host checks the turn's
provenance, not magic wording. A later move from `Blocked`, `Done`, or
`Rejected` to `Human Approved` can start a new execution attempt.

`In Progress` remains lease-managed, so neither the human nor agent sets it
through the generic tool. A direct Linear UI move is handled only through the
authenticated user-transition path above.

If the provider result is uncertain after a network failure, use
`linear_reconcile_work_item` before retrying a mutation.

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
ignored. Comments, issue changes, and messages in the corresponding Discord
thread share one ordered conversation.

A `Ready for Planning` update belongs to the planning controller. A `Human
Approved` update acquires the execution lease before the host admits a durable
isolated task. A user-authored state transition directly to `In Progress`
acquires the same lease without another provider move. An unleased `In Progress`
update without that actor and changed-field evidence remains controller-owned
but does not authorize work. These issue updates don't also start ordinary
conversation turns, because that would race a second agent against the durable
task. An `Awaiting Plan Approval` update waits for human review. A `Done`
delivery completes the linked active, blocked, or review-ready execution. Other
authenticated events provide context but don't bypass authorization.

The route verifies Linear's HMAC-SHA256 signature, requires a timestamp within
60 seconds, checks the configured organization ID, and deduplicates delivery
UUIDs. The signing secret never enters the agent container. The selected Linear
account's `public_source` setting determines whether callback content is
trusted.

One Temporal schedule reconciles every managed board once per minute, including
webhook-routed boards. It creates or recovers planning tasks for `Ready for
Planning`, leases `Human Approved` work, repairs an `In Progress` execution
whose durable task is missing, and admits `Follow-ups` without a second approval
lease. `Awaiting Plan Approval` remains idle until a human decides.

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
resumes from its durable checkpoint. Planning and execution tasks track their
own checkpoints and never replace the routed issue conversation's interactive
agent session.

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
| `linear_get_issue` | Gets an issue by stable Linear ID. |
| `linear_create_issue` | Creates an ordinary issue without an approval-bearing state. |
| `linear_list_todos` | Lists open items on the current workspace board. |
| `linear_create_todo` | Creates an unapproved workspace proposal. |
| `linear_create_comment` | Adds an ordinary comment to an issue. |
| `linear_create_attachment` | Attaches an external URL, including every pull request produced by the work, to an issue. |
| `linear_find_issues_by_attachment_url` | Resolves an exact external URL, such as a PR URL from a GitHub event, back to attached issues. |

Pynchy's built-in agent tools expose generic state actions plus the
host-managed lease record:

| Tool | Purpose |
|------|---------|
| `linear_submit_plan` | Atomically persists an initial or revised plan and leaves it `Awaiting Plan Approval`. |
| `linear_reconcile_work_item` | Resolves an uncertain provider transition. |
| `linear_list_work_items` | Lists durable execution records for the workspace. |
| `linear_move_todo` | Moves work to an agent-managed outcome or a directly human-authorized state. |

## Inspect executions

Operators can inspect the read-only work-item projection through:

```text
GET /work-items?workspace=<workspace>&limit=100
```

The response includes the issue, workspace, turn and task links, lifecycle
state, evidence, delivery outcome, and timestamps. It doesn't fetch issue
descriptions.

Linear is a public sink by default because issue creation sends data outside
Pynchy. A deployment that trusts its private Linear workspace and every member
can set `public_sink = false`. Workspace policy can still require approval
before an agent uses the tool.

To extend work-item behavior beyond the built-in integration, see
[Plugin authoring](../plugins/index.md).
