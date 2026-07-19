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
| `Awaiting Plan Approval` | A concrete plan exists, but execution lacks approval | Agent after planning |
| `Human Approved` | A human explicitly authorized execution | Human |
| `In Progress` | Pynchy claimed the approved item and started work | Pynchy lifecycle |
| `Blocked` | Execution needs intervention | Pynchy lifecycle |
| `Done` | Pynchy completed and verified the work | Pynchy lifecycle |
| `Rejected` | A human declined the proposal | Human |

`Ready for Planning` never authorizes execution. Pynchy can claim only a
`Human Approved` item. Agent tools cannot set `Ready for Planning`,
`Human Approved`, or `Rejected`; change those states in Linear to record the
human decision.

## Available tools

The built-in MCP server provides planning and browsing tools:

| Tool | Purpose |
|------|---------|
| `linear_list_teams` | Lists Linear teams visible to the API key. Use this first to find the `team_id`. |
| `linear_list_issues` | Lists recent issues, optionally scoped by `team_id`. |
| `linear_create_issue` | Creates an issue with `team_id`, `title`, and optional `description`, `project_id`, and `label_ids`. It cannot choose an approval-bearing workflow state. |
| `linear_list_todos` | Lists open Linear todo issues for the current Pynchy workspace. |
| `linear_create_todo` | Creates a workspace work item in `Agent Proposed`. |

Pynchy exposes lifecycle tools through its built-in agent tools MCP server. Use
them when an agent starts or finishes work from a workspace board:

| Tool | Purpose |
|------|---------|
| `linear_claim_work_item` | Claims a `Human Approved` issue for the current Pynchy execution and moves it to `In Progress`. |
| `linear_complete_work_item` | Completes a claimed item and records a completion summary. |
| `linear_block_work_item` | Moves a claimed item to Blocked and records the blocker. |
| `linear_handoff_work_item` | Moves a claimed item to Blocked, records the next owner, and releases Pynchy's claim. |
| `linear_reconcile_work_item` | Resolves an uncertain provider outcome by checking Linear instead of retrying the mutation blindly. |
| `linear_list_work_items` | Lists durable Pynchy execution records for the current workspace. |
| `linear_move_todo` | Moves an unlinked item to `agent_proposed` or `awaiting_plan_approval`. It rejects approval-bearing targets and items with an active Pynchy claim. |

## Execute a work item

Use a `Human Approved` issue as explicit permission to execute. An agent claims
the issue before it performs the work. Pynchy stores the workspace,
Linear issue link, current turn, scheduled-task ID when applicable, observed
Linear state, attempt number, evidence references, and the requested transition
before writing to Linear.

One active Pynchy execution can own an issue. A second claim fails with the
existing execution record instead of creating duplicate work. Completion,
blocking, and handoff act only on that linked execution. A handoff releases the
claim so another owner can pick the item up deliberately.

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
