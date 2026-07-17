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
```

`target_profile` must name a configured Pynchy profile for the dedicated test
target and labels that target in stored evidence. Each executable scenario
registers an exercise, an independent verifier, and an idempotent cleanup
step. The runner retries cleanup separately. A declared scenario without an
executable implementation records `not_established`; it never reports a false
pass.

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

## Built-in catalog and CI gate

`src/pynchy/actions.py` provides the built-in `ACTION_SPECS` catalog. Each
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

Run the complete hermetic contract with:

```bash
uv run pytest --action-coverage
```

The gate fails when a registered action has no marked test or when a test uses
an unknown action ID. Pre-commit runs the same command. Mark tests that prove
the action's behavior; do not add a marker to a test that only checks tool
visibility or a data class.

## Adding an action

1. Split the capability into observable state transitions.
2. Add an `ActionSpec` with a stable dotted ID and the implementation owner.
3. Add or identify behavioral hermetic tests and mark them with the action ID.
4. For a provider-dependent effect, select `hermetic_and_agentic` and name a
   scenario that includes independent verification and cleanup.
5. Run `uv run pytest --action-coverage` before submitting the change.

The built-in catalog covers calendars, memories, task lifecycle, todos,
outbound delivery, vault-backed skill discovery and access, workspace and
deployment operations, desktop controls, Google and Slack setup, X actions,
and first-party Linear, Proton Mail, and notebook MCP actions. Third-party
MCP servers remain outside the package's static tool inventory; their plugin
tests need to establish their own action evidence at the provider boundary.
