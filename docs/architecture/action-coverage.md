# Action coverage

Use the action catalog to prove that Pynchy can perform every supported
user-meaningful state transition. It connects each action to behavioral tests
and, when needed, a real-service canary.

An action describes an effect, not a tool name. For example, sending an email,
searching for it, reading it, and deleting it each form separate actions. A
send-to-self check composes those actions into one scenario.

## Evidence levels

Every cataloged action needs hermetic coverage. Tests exercise Pynchy's own
validation, policy, idempotency, error handling, and state changes against a
fake provider when an external service participates.

Actions marked `hermetic_and_agentic` also need a real-service canary. A
canary runs with a dedicated test identity and workspace, uses tagged test
data, verifies the remote effect independently of the agent's prose, and
cleans up the test artifact. The scheduled canary runner records the result
and reports a failure only after the same scenario previously passed on that
target profile.

Declaring a canary scenario does not claim that the real-service check exists
or passed. It records the missing evidence so the implementation cannot hide
behind hermetic tests.

## Agentic canary runner

Enable scheduled real-service canaries only after configuring a dedicated test
identity and workspace. Never point this target at a personal account or a
production workspace.

```toml
[canary]
enabled = true
target_profile = "external-canary"
schedule = "0 5 * * *"
scenario_ids = [
  "desktop.computer.round.trip",
  "calendar.round.trip",
  "calendar.google.round.trip",
  "drive.google.round.trip",
  "linear.workspace.round.trip",
  "proton.mail.round.trip",
  "security.deterministic.hard-block",
  "security.file-secret-taint",
  "security.approval.mutation-replay",
  "security.cop.degraded-approval",
  "security.gateway.posture",
  "security.package.metadata",
]
calendar_name = "pynchy-canary"
google_calendar_server = "gcal.canary"
google_calendar_id = "pynchy-canary"
google_drive_server = "gdrive.canary"
google_drive_probe_query = "pynchy-canary-fixture"
google_drive_file_id = "permanent-fixture-file-id"
linear_team_key = "PYNCHY"
linear_workspace = "canary-workspace"
proton_mailbox = "INBOX"
proton_recipient = "canary-mailbox@example.com"
```

`target_profile` must name a configured Pynchy profile that enables every
selected tool and labels that target in stored evidence. `scenario_ids` makes
the operational scope explicit; unselected scenarios do not run. Calendar
checks require a dedicated `calendar_name`. Linear checks require a dedicated
test team and workspace, then permanently delete their test issues. Proton
Mail checks use the named mailbox and a dedicated `proton_recipient`: they
send a tagged message, list and read that message after delivery, then
permanently delete it and independently confirm its absence. Each executable
scenario registers an exercise, an independent verifier, and an idempotent
cleanup step. The runner retries cleanup separately. A declared scenario
without an executable implementation records `not_established`; it never
reports a false pass.

The desktop check requires `computer_use` on the target profile. It captures
one screenshot through the configured provider, verifies the attributed host
artifact and byte count, then removes the screenshot.

Google Calendar checks use the named managed MCP server and dedicated calendar:
they list provider tools and calendars, create a tagged event with a generated
event ID, retrieve it through a fresh MCP session, then delete it without
sending attendee updates and confirm it cannot be retrieved. Google Drive is
intentionally read-only: its check proves the provider's published search and
read tools against a configured harmless fixture file, without creating or
changing remote content. The exact server names make multi-account targets
unambiguous.

The built-in operational checks cover desktop screenshot capture, CalDAV and
Google Calendar lifecycle, Google Drive search/read access, Linear issue and
workspace-todo lifecycle, and Proton Mail delivery, receipt, read, and
deletion. Credential setup, token refresh, social posting, and channel
interaction remain separate scenarios because they need dedicated test
targets.

Six harmless local scenarios use the same runner, durable evidence rows,
regression handling, report, and history endpoints to exercise deterministic
blocking, credential-file taint, approval mutation and replay rejection,
degraded Cop approval, gateway redaction posture, and package metadata
fresh/degraded behavior. They create no external artifact and require no tool
on the target profile. Report rows label them with
`evidence_kind = "security_assurance"` and an empty `action_ids` tuple. They
supplement operational security evidence; they do not pretend to prove a
semantic user action or satisfy `pytest --action-coverage`.

Pynchy stores every run with its scenario and action IDs, target profile, code
and configuration revisions, timestamps, outcome, redacted error class, and
safe evidence references. A failing first attempt remains an establishment
failure. A failing run after a prior pass starts a regression, sends an admin
notice, and appears in the report until a later pass records recovery.

Use these HTTP surfaces to inspect the current report and durable history:

- `GET /canaries/report` returns declared scenarios, their latest evidence,
  unresolved regressions, and recent results.
- `GET /canaries/runs?scenario_id=<id>&limit=<1-200>` returns filtered run
  history. Omit `scenario_id` to list every scenario.

## Host-action descriptors and capability status

`HostActionDescriptor` is the executable companion to `ActionSpec`. It binds a
host handler to its stable capability ID, semantic action IDs, read/write
classification, approval expiry, idempotency rule, terminal audit contract,
requirements, setup guidance, and optional bounded probe. Plugins contribute
new semantic actions through `pynchy_action_specs`; built-in and plugin action
IDs are validated together at startup.

Pynchy resolves these descriptors per workspace into one of six states:

| State | Meaning |
| --- | --- |
| `ready` | Config, policy, probe, and required evidence currently pass |
| `unconfigured` | The workspace or required tool selection is missing |
| `unavailable` | A bounded provider or host prerequisite probe failed |
| `denied_by_policy` | Current semantic capability or tool-trust policy forbids it |
| `degraded` | The provider probe is degraded or established canary evidence regressed |
| `not_established` | Required real-service evidence has never passed for the target |

`GET /capabilities` returns every configured workspace snapshot.
`GET /capabilities?workspace=<name>` returns one workspace, and `/status`
includes the aggregate under `capabilities`. Reasons and remediation metadata
are safe to display; secret values and provider error bodies are not included.
`pynchy doctor [--workspace <name>]` renders the same endpoint for a person;
add `--json` for the unmodified snapshot. The CLI is a client of the running
service and does not resolve a second local view.

The snapshot is not an authorization token. Dispatch and approved replay look
up the current descriptor and re-run `SecurityGate`; provider handlers still
perform their real operation-time checks. A stale `ready` result cannot grant
access.

## Transactional external actions

Write descriptors can opt into an `ActionIntentContract`. Before a provider
call, Pynchy persists the canonical recipient and payload, source references,
policy decision, approval identity, and IPC idempotency key. It then claims and
marks the intent executing before the provider call. A provider receipt
confirms the intent; an interrupted or unreceipted call becomes
`outcome_unknown` during startup recovery and is never automatically replayed.

`GET /actions` exposes bounded, workspace-filterable operator state without
returning the draft payload. Matrix send is the first consumer; future external
writes must use the same lifecycle rather than inventing another retry ledger.

## Built-in catalog and CI gate

`src/pynchy/actions.py` provides the built-in `ACTION_SPECS` catalog. Plugins
may add owned specifications with `pynchy_action_specs`. Each
`ActionSpec` names an action ID, owner, summary, evidence requirement,
optional canary scenario, and the Pynchy tool or host workflow that exposes
it. The catalog check compares built-in agent tools with those mappings, so a
new tool must receive a semantic action before it becomes available.

Attach an action ID to a behavioral test with `pytest.mark.action`:

```python
@pytest.mark.action("calendar.event.create")
async def test_create_event_success():
    ...
```

Run the fast marker contract with:

```bash
uv run python scripts/prek_hooks/check_action_coverage.py
```

The repository gate fails when a built-in action has no marked test or when a
test uses an unknown built-in action ID. It also rejects dynamic, unattached,
or malformed action markers, then asks pytest to collect only the files that
contain valid markers. Prek and GitHub CI run this command.

The full test suite can independently repeat the dynamic collection check:

```bash
uv run pytest --action-coverage
```

The manual full-suite Prek hook does not add this flag because the fast gate
already runs the authoritative marker-file collection and the full tests
should execute only once.
Third-party packages must apply the same `assess_hermetic_coverage` contract to
their contributed specifications in their own CI. Mark tests that prove the
action's behavior; do not add a marker to a test that only checks tool
visibility or a data class.

## Adding an action

1. Split the capability into observable state transitions.
2. Add an `ActionSpec` with a stable dotted ID and the implementation owner.
3. Add or identify behavioral hermetic tests and mark them with the action ID.
4. For a provider-dependent effect, select `hermetic_and_agentic` and name a
   scenario that includes independent verification and cleanup.
5. Run `uv run python scripts/prek_hooks/check_action_coverage.py` before
   submitting the change.

The built-in catalog covers calendars, memories, task lifecycle, todos,
outbound delivery, personalized skill discovery and access, workspace and
deployment operations, desktop controls, Google and Slack setup, X actions,
and first-party Linear, Proton Mail, and notebook MCP actions. Provider MCP
servers with user-facing operational effects must also declare semantic actions
and exercise their own provider boundary; a third-party implementation is not
an exemption from runtime evidence.
