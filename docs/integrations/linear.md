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

At startup, Pynchy creates missing workspace projects and workflow states.
Each project carries an immutable workspace marker. Runtime reads and writes
require exactly one marked project and fail closed if the project is missing
or ambiguous.

Reconciliation is additive. Pynchy doesn't delete or archive existing Linear
objects.

| State | Meaning |
|-------|---------|
| `Agent Proposed` | The agent suggested work. No human authorized it. |
| `Human Approved` | A human explicitly authorized execution. |
| `In Progress` | The host owns an active execution lease. |
| `Awaiting Review` | The delivered result awaits review. For code, every PR is attached to the issue. |
| `Follow-ups` | The main result is ready; the agent is finishing operational loose ends. |
| `Blocked` | The result needs intervention or another owner. |
| `Done` | The agent judges the complete job, including follow-ups, finished. |
| `Rejected` | A human declined the proposal. |

Human authorization and active ownership are host-enforced boundaries. Planning,
investigation, implementation, validation, review, follow-up, and completion
belong to the agent.

### Authorize work

Move an existing proposal to `Human Approved` in Linear, or explicitly request
the state change in a direct human conversation. The agent can then use
`linear_move_todo`; Pynchy verifies that the current turn came directly from a
human without requiring a matching quote or prescribed wording.

`linear_create_todo` creates an unapproved `Agent Proposed` item. An autonomous
agent can't promote its own proposal or set `Rejected`.

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
through the generic tool.

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

A `Human Approved` issue delivery acquires the execution lease before the host
starts an agent turn. A `Done` delivery completes the linked active, blocked,
or review-ready execution. Other authenticated events provide context but
don't bypass authorization.

The route verifies Linear's HMAC-SHA256 signature, requires a timestamp within
60 seconds, checks the configured organization ID, and deduplicates delivery
UUIDs. The signing secret never enters the agent container. The selected Linear
account's `public_source` setting determines whether callback content is
trusted.

When a workspace has no webhook route, the host polls `Human Approved`,
recoverable `In Progress`, and `Follow-ups` items once per minute. Execution
polling and webhooks share the same lease boundary; follow-up work doesn't need
a second approval lease. A webhook-routed workspace isn't also polled.

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
| `linear_reconcile_work_item` | Resolves an uncertain provider transition. |
| `linear_list_work_items` | Lists durable execution records for the workspace. |
| `linear_move_todo` | Moves work to an agent-managed outcome or a directly human-authorized state. |

## Migrate an existing board

Older installations might retain `Ready for Planning` and
`Awaiting Plan Approval`. Pynchy no longer creates or observes those states and
doesn't delete them automatically. Boot reconciliation adds `Follow-ups` when
it is missing.

Move unapproved items to `Agent Proposed`. Move only explicitly authorized work
to `Human Approved`. Existing `In Progress`, `Awaiting Review`, `Blocked`,
`Done`, and `Rejected` items keep their meaning; use `Follow-ups` for final
operational work after the main deliverable is ready.

Deployment-specific prompts aren't changed by repository upgrades. Remove fixed
planning, claiming, commit, sync, or completion rituals from files under
`data/personalization/prompts/`. State the objective, authority, and success
condition instead. Keep only constraints that the host can't enforce or the
agent can't reasonably infer.

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
