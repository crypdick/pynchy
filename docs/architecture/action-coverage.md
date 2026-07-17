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
cleans up the test artifact. The future canary runner records the result and
reports a failure only after the same action previously passed on that target.

Declaring a canary scenario does not claim that the real-service check exists
or passed. It records the missing evidence so the implementation cannot hide
behind hermetic tests.

## Built-in catalog and CI gate

`src/pynchy/actions.py` provides the built-in `ACTION_SPECS` catalog. Each
`ActionSpec` names an action ID, owner, summary, evidence requirement, and
optional canary scenario.

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

The catalog currently covers the first action families with established
behavioral tests: calendars, memories, task lifecycle, todos, outbound
delivery, and ask-user. Subsequent increments add the remaining tool,
integration, security, workspace, and host-operation families.
