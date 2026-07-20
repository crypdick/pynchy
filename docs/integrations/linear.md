# Linear

The built-in Linear integration gives every Pynchy workspace a durable Linear
todo board. Each workspace maps to a Linear Project named from the workspace
label, using the folder title for repo-slug labels (`Code Improver` for
`code-improver`). Explicit workflow names distinguish an agent suggestion,
planning readiness, and human authorization to execute.

## Configure access

Create a Linear personal API key, then store it in the host environment as
`LINEAR_API_KEY`. Pynchy uses that key only as a credential; it does not grant
workspace access to Linear tools by itself.

If the key can see multiple teams, set `LINEAR_TEAM_KEY` to the team key, team
ID, or exact team name Pynchy should use:

```bash
LINEAR_API_KEY=lin_api_...
LINEAR_TEAM_KEY=SYN
```

Select the Linear capability through a profile. For Pynchy's workspace todo-board
integration, declare a Linear tool and select it from the profile:

```toml
[tools.linear]
type = "linear"
public_source = false
secret_data = false
public_sink = true
dangerous_writes = false

[profiles.project]
tools = ["linear"]

[workspaces.code-improver]
profiles = ["project"]
```

To expose the Linear MCP tools directly to agents, select the plugin-provided
MCP runtime by declaring and granting the `linear` tool:

```toml
[tools.linear]
type = "linear"
project_per_workspace = true

[profiles.project]
tools = ["linear"]

[workspaces.code-improver]
profiles = ["project"]
```

Editing `.env` triggers the normal Pynchy auto-restart; do not restart the
service manually unless the health check shows it is stuck.

## Workspace boards

On boot, Pynchy reconciles Linear state for all registered workspaces:

| Pynchy workspace | Linear object |
|------------------|---------------|
| Workspace label | Project named from the workspace, such as `Code Improver` |
| `todo ...` messages | Issues in that workspace project |
| Todo status | Team workflow state |

Boot reconciliation is additive only. Pynchy creates missing projects and states,
and renames older Pynchy-managed projects when their description contains the
matching `pynchy.workspace=...` marker. It does not delete, archive, assign, or
otherwise clean up Linear objects automatically.

Only boot reconciliation provisions projects and workflow states. Runtime reads,
todo creation, and lifecycle actions require exactly one project carrying the
workspace marker and never create or repair provider resources. Missing or
duplicate projects fail closed so a transient or ambiguous provider response
cannot silently create another board.

When a user sends `todo ...` while a workspace task is running, a Linear-enabled
workspace creates the issue in `Ready for Planning` without writing a second
local todo. Workspaces without Linear keep the local todo path. Agent-created
workspace items start in `Agent Proposed`.

## Approval workflow

Linear holds the complete planning and authorization state:

| State | Meaning | Who advances it |
|-------|---------|-----------------|
| `Agent Proposed` | Pynchy identified an opportunity; no human endorsed it | Agent creates the item |
| `Ready for Planning` | A human wants Pynchy to develop the idea into a plan | Human |
| `Awaiting Plan Approval` | The issue description contains a concrete Pynchy plan, but execution lacks approval | Agent through `linear_submit_plan` |
| `Human Approved` | A human explicitly authorized execution | Human |
| `In Progress` | Pynchy claimed the approved item and started work | Pynchy lifecycle |
| `Awaiting Review` | Pynchy opened a GitHub pull request and linked it to the execution | Pynchy lifecycle |
| `Blocked` | Execution needs intervention | Pynchy lifecycle |
| `Done` | The linked GitHub pull request merged | Authenticated GitHub webhook |
| `Rejected` | A human declined the proposal | Human |

`Ready for Planning` never authorizes execution. Pynchy can claim only a
`Human Approved` item. Agent tools cannot set `Ready for Planning`,
`Human Approved`, `Done`, or `Rejected`; change the human decision states in
Linear. Pynchy reserves `Done` for merge evidence from GitHub rather than an
agent's completion claim.

## Receive Linear callbacks

Pynchy can route Linear comments and issue changes into durable issue
conversations. Configure one route per Linear webhook subscription. The named
workspace must select a Linear tool and point to a Discord guild channel where
Pynchy can create issue threads:

```toml
[[plugins.linear.options.webhook_routes]]
name = "code-improver"
workspace = "code-improver"
secret_env = "LINEAR_WEBHOOK_SECRET"  # pragma: allowlist secret
organization_id = "your-linear-organization-id"
```

Store the signing secret shown by Linear in the host `.env`:

```bash
LINEAR_WEBHOOK_SECRET=...
```

Expose Pynchy through a public HTTPS reverse proxy, following the
[control-plane public-bind setup](../usage/control-plane.md#enable-remote-diagnostic-access).
Then create a Linear webhook for `Comment` and `Issue` events with this URL:

```text
https://pynchy.example.com/webhooks/linear/code-improver
```

The final path component is the configured route `name`. Pynchy does not
provision TLS or a public hostname. Linear requires a public, non-localhost
HTTPS URL; see the [Linear webhook documentation](https://linear.app/developers/webhooks).

Every schema-valid `create`, `update`, or `remove` delivery for a `Comment` or
`Issue` enters the issue's conversation. Comments do not need to mention
`@pynchy`. Pynchy uses the immutable Linear issue ID for conversation identity,
so comments and edits for one issue reuse one agent session and one Discord
thread. Different issues can run concurrently. Durable delivery IDs prevent
duplicate turns.

Pynchy creates a readable thread title such as `[PYN-123] Repair scheduler`
without using that mutable title as identity. If the thread disappears, the next
delivery creates a replacement and rebinds the existing issue conversation and
session. Messages that people send in the Discord thread join that same context;
they do not become Linear comments automatically.

Linear scopes webhook subscriptions to one team or all public teams, not to one
Project. Point the subscription at the team that owns the Pynchy board. When that
team contains other Projects, their events can wake the route too. Before acting,
the conversation turn lists the workspace's complete Pynchy board and stops
unless the event's issue belongs to that Project.

The callback only asks Pynchy to inspect current state. `Ready for Planning`
permits planning only. Execution still requires `linear_claim_work_item` to claim
the exact `Human Approved` issue. A comment, issue edit, or Pynchy-authored state
transition never grants execution authority by itself. Ordinary agent output
stays in Discord; only an explicit Linear action mutates the issue.

The route verifies Linear's HMAC-SHA256 signature against the raw request body,
requires matching millisecond timestamps within 60 seconds, checks the optional
organization ID, and deduplicates the `Linear-Delivery` UUID. The signing secret
never enters the agent container. Each schema-valid authenticated delivery leaves
a durable receipt, then idempotently links to the issue's per-conversation FIFO
before Pynchy returns `200` to Linear. A replay repairs a crash between those two
writes without creating another turn. One issue processes deliveries in receipt
order; successful turn finalization wakes the next queued delivery only after the
claim and message cursor commit together. Each turn confirms workspace-board
ownership, fetches the issue's current Linear state, and treats the callback body
as untrusted context before deciding what to do.

Linear recommends webhooks instead of API polling for update-driven integrations.
When a Linear-enabled workspace has no webhook route, Pynchy keeps the approval
workflow functional on a local-only host: once per minute, the host queries only
the shared `Ready for Planning` and `Human Approved` states and admits one
deterministic isolated task for each newly observed issue on a managed workspace
Project. A planning task must persist its plan through `linear_submit_plan`; an
execution task must claim the issue before it changes code. Configure a webhook
route for lower latency and to wake on comments and other issue changes. Pynchy
excludes webhook-routed workspaces from this fallback query so one workspace does
not use both delivery paths.

## Schedule proactive proposals

Use a config-backed [agent task](../usage/scheduled-tasks.md#agent-tasks) to run a real
isolated review instead of posting a reminder into an interactive session. The
bundled `prompts/pynchy-proactive-review.md` prompt reviews one bounded slice,
deduplicates against the workspace board, and creates up to three rich
`Agent Proposed` items. It never approves or executes them.

Select the Pynchy repository and Linear tool on a dedicated private workspace:

```toml
[profiles.proactive-review]
includes = ["base"]
repo = "owner/pynchy"
tools = ["linear"]

[workspaces.proactive-review]
profiles = ["proactive-review"]

[jobs.pynchy-proactive-review]
enabled = true
schedule = "0 10 * * 1"
workspace = "proactive-review"
prompt_file = "prompts/pynchy-proactive-review.md"
```

The example runs each Monday at 10:00 in Pynchy's configured timezone. Linear
remains the canonical proposal and approval surface; the job does not maintain
a second backlog or approval ledger.

## Available tools

The built-in MCP server provides planning and browsing tools:

| Tool | Purpose |
|------|---------|
| `linear_list_teams` | Lists Linear teams visible to the API key. Use this first to find the `team_id`. |
| `linear_list_issues` | Lists recent issues, optionally scoped by `team_id`. |
| `linear_get_issue` | Gets one issue by its stable Linear ID. |
| `linear_create_issue` | Creates an issue with `team_id`, `title`, and optional `description`, `project_id`, and `label_ids`. It cannot choose an approval-bearing workflow state. |
| `linear_list_todos` | Lists open Linear todo issues for the current Pynchy workspace. |
| `linear_create_todo` | Creates a workspace work item in `Agent Proposed`, with an optional Markdown description and Linear priority. |

Pynchy exposes lifecycle tools through its built-in agent tools MCP server. Use
them when an agent starts or finishes work from a workspace board:

| Tool | Purpose |
|------|---------|
| `linear_submit_plan` | Writes a concrete Markdown plan into a `Ready for Planning` issue and atomically moves it to `Awaiting Plan Approval`; it never authorizes execution. |
| `linear_claim_work_item` | Claims a `Human Approved` issue for the current Pynchy execution and moves it to `In Progress`. |
| `linear_await_review_work_item` | Moves a claimed item to `Awaiting Review`, records a summary, and links its GitHub pull-request URL. |
| `linear_block_work_item` | Moves a claimed item to Blocked and records the blocker. |
| `linear_handoff_work_item` | Moves a claimed item to Blocked, records the next owner, and releases Pynchy's claim. |
| `linear_reconcile_work_item` | Resolves an uncertain provider outcome by checking Linear instead of retrying the mutation blindly. |
| `linear_list_work_items` | Lists durable Pynchy execution records for the current workspace. |
| `linear_move_todo` | Returns an unlinked item to `agent_proposed`. Planning tasks must use `linear_submit_plan` so the plan and state change stay coupled. It rejects decision-bearing targets and items with an active Pynchy claim. |

## Execute a work item

Use a `Human Approved` issue as explicit permission to execute. An agent claims
the issue before it performs the work. Pynchy stores the workspace,
Linear issue link, current turn, scheduled-task ID when applicable, observed
Linear state, attempt number, evidence references, and the requested transition
before writing to Linear.

One active Pynchy execution can own an issue. A second claim fails with the
existing execution record instead of creating duplicate work. Review
submission, blocking, and handoff act only on that linked execution. A handoff
releases the claim so another owner can pick the item up deliberately.

When the implementation is ready, call `linear_await_review_work_item` with the
canonical `https://github.com/<owner>/<repository>/pull/<number>` URL. The claim
stays active while the issue is in `Awaiting Review`. If that repository's
[GitHub webhook route](github.md) maps to the same workspace, an authenticated
merged-PR delivery moves the linked issue to `Done` and releases the claim.
Opening the PR, converting it from draft, or closing it without merging does not
complete the work item.

If a network failure happens after Pynchy sends a Linear mutation, Pynchy marks
the transition unknown. Use `linear_reconcile_work_item` to inspect provider
state. Do not repeat the original lifecycle command until reconciliation shows
that Linear did not receive it.

Managed flows remain separate from this lifecycle. A future managed flow can
link its flow ID to a work-item execution without turning every Linear issue
into a scheduler workflow.

## Inspect work-item executions

Operators can inspect the same read-only projection through the control plane:

```text
GET /work-items?workspace=<workspace>&limit=100
```

The response includes the Linear URL and identifier, workspace, turn and task
links, lifecycle state, transition-relevant observed state, blocker or handoff
owner, requester-delivery outcome, and timestamps. It does not fetch or expose
issue descriptions.

The built-in plugin conservatively marks Linear as a public sink because issue
creation sends data outside Pynchy. A deployment that treats its private Linear
workspace and every workspace member as trusted can configure
`public_sink = false`. Workspace security policy can still require approval
before agents use the tool.

## Backlog migration

This integration only adds the Linear task-tracking capability. Migrating
`backlog/TODO.md` and its plan files into Linear remains separate follow-up work.
