"""Focused tests for small public parsing and projection contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pluggy
import pytest

import pynchy.plugins
from pynchy.host.orchestrator.connection_runtime_owner import ConnectionRuntimeOwner
from pynchy.host.orchestrator.job_gates import parse_wake_agent_gate
from pynchy.host.orchestrator.webhook_event_rendering import prompt_for_event
from pynchy.plugins.api import PynchySpec, WebhookEvent, WebhookRoute, load_connection_runtimes
from pynchy.plugins.computer_use.artifacts import screenshot_artifact
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_conversation_identity import (
    resolve_linear_issue_conversation,
)
from pynchy.plugins.integrations.linear_mutation_effects import (
    LinearSelfEchoRecorder,
    LinearWebhookEffectAttempt,
)
from pynchy.plugins.integrations.linear_plan_admission import review_approved_plan
from pynchy.plugins.integrations.linear_planning_tasks import (
    LinearPlanningTaskRuntime,
    admit_planning_issue,
    configure_linear_planning_task_runtime,
)
from pynchy.plugins.integrations.linear_self_echoes import linear_self_echo_recorder
from pynchy.plugins.integrations.linear_work_item_tasks import (
    DecisionIssue,
    LinearWorkItemTaskRuntime,
    configure_linear_work_item_task_runtime,
    decision_state_id,
    ensure_task_active,
    get_conversation_control_binding,
)
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state.work_item_rows import row_to_transition
from pynchy.webhook_effects import WebhookEffectId
from pynchy.workspace.api import WorkspaceProfile


class _MissingPlanClient:
    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        return {}

    async def get_issue(self, _issue_id: str) -> dict[str, object] | None:
        return None

    async def create_comment(self, _issue_id: str, _body: str) -> dict[str, object]:
        return {}


def test_wake_gate_ignores_payloads_without_a_wake_decision() -> None:
    assert parse_wake_agent_gate('{"status": "ok"}') is None


def test_wake_gate_rejects_a_non_json_final_line() -> None:
    assert parse_wake_agent_gate('{"wakeAgent": true}\nnot-json') is None


def test_wake_gate_returns_none_for_empty_output() -> None:
    assert parse_wake_agent_gate("\n  \n") is None


def test_plugin_namespace_rejects_unknown_attributes() -> None:
    with pytest.raises(AttributeError, match="no attribute"):
        _ = pynchy.plugins.not_a_plugin


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([], TypeError),
        ({"state": "not-an-object", "project": {}}, TypeError),
        ({"state": {}, "project": "not-an-object"}, TypeError),
    ],
)
def test_decision_issue_rejects_malformed_payloads(payload: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        DecisionIssue.from_payload(payload)


def test_decision_issue_without_project_is_not_a_managed_issue() -> None:
    assert DecisionIssue.from_payload({"state": {}, "project": None}) is None


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [("id", 42, TypeError), ("id", "", ValueError)],
)
def test_decision_issue_requires_nonempty_text_fields(
    key: str, value: object, error: type[Exception]
) -> None:
    payload = {
        "id": "issue-1",
        "identifier": "SYN-1",
        "title": "Work",
        "url": "https://linear.app/issue/SYN-1",
        "updatedAt": "2026-07-31T00:00:00Z",
        "state": {"id": "state-1"},
        "project": {"id": "project-1"},
    }
    payload[key] = value

    with pytest.raises(error):
        DecisionIssue.from_payload(payload)


def test_linear_decision_state_id_rejects_missing_or_non_text_state() -> None:
    with pytest.raises(ValueError, match="lacks decision state"):
        decision_state_id(LinearWorkspaceBoard(team={}, project={}, states={}), "approved")
    with pytest.raises(TypeError, match="lacks a text ID"):
        decision_state_id(
            LinearWorkspaceBoard(team={}, project={}, states={"approved": {"id": 42}}),
            "approved",
        )


def test_connection_runtime_loader_ignores_empty_plugin_contributions() -> None:
    plugin_manager = pluggy.PluginManager("pynchy")
    plugin_manager.add_hookspecs(PynchySpec)

    assert load_connection_runtimes(plugin_manager) == []


@pytest.mark.asyncio
async def test_linear_conversation_resolution_requires_configuration(monkeypatch) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_conversation_identity._runtime.runtime", None
    )

    with pytest.raises(RuntimeError, match="Linear conversation runtime has not been configured"):
        await resolve_linear_issue_conversation("issue-1", "workspace", "account")


@pytest.mark.asyncio
async def test_linear_work_item_binding_requires_configuration(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.linear_work_item_tasks._runtime.runtime", None)

    with pytest.raises(
        RuntimeError,
        match="Linear work-item task runtime has not been configured",
    ):
        await get_conversation_control_binding("conversation-1")


@pytest.mark.asyncio
async def test_linear_task_recovery_ignores_invalid_last_run_timestamp() -> None:
    existing = ScheduledTask(
        id="task-1",
        group_folder="project",
        chat_jid="linear:project",
        prompt="old",
        schedule_type="once",
        schedule_value="2026-07-31T00:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        status="completed",
        last_run="not-a-timestamp",
        derived_thread_name="[SYN-1] Work",
    )
    get_task = AsyncMock(return_value=existing)
    update_task = AsyncMock()
    runtime = LinearWorkItemTaskRuntime(
        get_control_binding=AsyncMock(),
        get_task=get_task,
        create_task=AsyncMock(),
        update_task=update_task,
        get_task_logs=AsyncMock(),
        bind_execution_to_task=AsyncMock(),
        get_active_execution=AsyncMock(),
        resume_once_task=AsyncMock(),
        get_execution_for_issue=AsyncMock(),
    )

    configure_linear_work_item_task_runtime(runtime)
    recovered, admitted = await ensure_task_active(
        existing,
        observed_at=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
    )

    assert recovered == existing
    assert admitted is False
    update_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_linear_task_recovery_treats_naive_recent_last_run_as_utc() -> None:
    existing = ScheduledTask(
        id="task-1",
        group_folder="project",
        chat_jid="linear:project",
        prompt="old",
        schedule_type="once",
        schedule_value="2026-07-31T00:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        status="completed",
        last_run="2026-07-31T00:04:00",
        derived_thread_name="[SYN-1] Work",
    )
    get_task_logs = AsyncMock()
    runtime = LinearWorkItemTaskRuntime(
        get_control_binding=AsyncMock(),
        get_task=AsyncMock(return_value=existing),
        create_task=AsyncMock(),
        update_task=AsyncMock(),
        get_task_logs=get_task_logs,
        bind_execution_to_task=AsyncMock(),
        get_active_execution=AsyncMock(),
        resume_once_task=AsyncMock(),
        get_execution_for_issue=AsyncMock(),
    )

    configure_linear_work_item_task_runtime(runtime)
    recovered, admitted = await ensure_task_active(
        existing,
        observed_at=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
    )

    assert recovered == existing
    assert admitted is False
    get_task_logs.assert_not_awaited()


@pytest.mark.asyncio
async def test_linear_task_recovery_refreshes_routing_ownership() -> None:
    existing = ScheduledTask(
        id="task-1",
        group_folder="old-project",
        chat_jid="linear:old-project",
        prompt="old",
        schedule_type="once",
        schedule_value="2026-07-31T00:00:00+00:00",
        session_policy=SessionPolicy.RESET_BEFORE_RUN,
        status="active",
        conversation_id="conversation-old",
        derived_thread_name="old thread",
    )
    update_task = AsyncMock()
    runtime = LinearWorkItemTaskRuntime(
        get_control_binding=AsyncMock(),
        get_task=AsyncMock(return_value=existing),
        create_task=AsyncMock(),
        update_task=update_task,
        get_task_logs=AsyncMock(),
        bind_execution_to_task=AsyncMock(),
        get_active_execution=AsyncMock(),
        resume_once_task=AsyncMock(),
        get_execution_for_issue=AsyncMock(),
    )

    configure_linear_work_item_task_runtime(runtime)
    proposed = ScheduledTask(
        id="task-1",
        group_folder="new-project",
        chat_jid="linear:new-project",
        prompt="new",
        schedule_type="once",
        schedule_value="2026-07-31T00:05:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        conversation_id="conversation-new",
        derived_thread_name="new thread",
    )

    refreshed, admitted = await ensure_task_active(
        proposed,
        observed_at=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
    )

    assert refreshed.session_policy is SessionPolicy.CONTINUE
    assert refreshed.conversation_id == "conversation-new"
    assert refreshed.derived_thread_name == "new thread"
    assert admitted is False
    update_task.assert_awaited_once_with(
        "task-1",
        {
            "session_policy": SessionPolicy.CONTINUE,
            "conversation_id": "conversation-new",
            "derived_thread_name": "new thread",
        },
    )


def test_linear_self_echo_recorder_requires_configuration(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.linear_self_echoes._runtime.runtime", None)

    with pytest.raises(RuntimeError, match="Linear self-echo runtime has not been configured"):
        linear_self_echo_recorder("account")


@pytest.mark.asyncio
async def test_linear_planning_admission_requires_configuration(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.linear_planning_tasks._runtime.runtime", None)
    issue = DecisionIssue(
        id="issue-1",
        identifier="SYN-1",
        title="Plan this",
        url="https://linear.app/issue/SYN-1",
        description="",
        updated_at="2026-07-29T00:00:00Z",
        state_id="ready",
        project_id="project-1",
    )

    with pytest.raises(RuntimeError, match="Linear planning-task runtime has not been configured"):
        await admit_planning_issue(
            issue,
            WorkspaceProfile(jid="group@g.us", name="Project", folder="project", trigger="@Bot"),
            observed_at=datetime(2026, 7, 29, tzinfo=UTC),
            public_source=True,
        )


@pytest.mark.asyncio
async def test_linear_planning_admission_keeps_trusted_source_unfenced(monkeypatch) -> None:
    issue = DecisionIssue(
        id="issue-1",
        identifier="SYN-1",
        title="Plan this",
        url="https://linear.app/issue/SYN-1",
        description="",
        updated_at="2026-07-29T00:00:00Z",
        state_id="ready",
        project_id="project-1",
    )
    workspace = WorkspaceProfile(jid="group@g.us", name="Project", folder="project", trigger="@Bot")
    configure_linear_planning_task_runtime(
        LinearPlanningTaskRuntime(get_all_tasks=AsyncMock(return_value=[]))
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_planning_tasks.linear_issue_conversation_id",
        AsyncMock(return_value="conversation-1"),
    )

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_planning_tasks.ensure_task_active",
        AsyncMock(side_effect=lambda task, *, observed_at: (task, True)),
    )

    task = await admit_planning_issue(
        issue,
        workspace,
        observed_at=datetime(2026, 7, 29, tzinfo=UTC),
        public_source=False,
    )

    assert task is not None
    assert task.input_source == "trusted:linear:ready_for_planning"
    assert json.loads(task.prompt)["issue_id"] == issue.id


@pytest.mark.asyncio
async def test_linear_effect_confirmation_requires_evidence() -> None:
    attempt = LinearWebhookEffectAttempt(
        recorder=LinearSelfEchoRecorder(
            account_name="account",
            begin=AsyncMock(),
            mark_executing=AsyncMock(),
            confirm=AsyncMock(),
            fail=AsyncMock(),
            mark_outcome_unknown=AsyncMock(),
        ),
        effect_id=WebhookEffectId("effect-1"),
    )

    with pytest.raises(ValueError, match="requires evidence"):
        await attempt.confirm(None)


@pytest.mark.asyncio
async def test_linear_plan_admission_rejects_missing_current_issue() -> None:
    assert (
        await review_approved_plan(
            _MissingPlanClient(),
            None,
            workspace="project",
            board=LinearWorkspaceBoard(team={}, project={}, states={}),
            issue_id="issue-1",
            identifier="SYN-1",
            title="Missing issue",
            url="https://linear.app/issue/SYN-1",
            description="plan",
            updated_at="2026-07-29T00:00:00Z",
            public_source=True,
        )
        is None
    )


def test_transition_row_rejects_non_object_receipts() -> None:
    row = {
        "id": "transition-1",
        "execution_id": "execution-1",
        "request_id": "request-1",
        "operation": "move",
        "target_status": "in_progress",
        "result_execution_status": "in_progress",
        "evidence_refs": "[]",
        "summary": None,
        "blocker": None,
        "handoff_to": None,
        "status": "pending",
        "receipt": "[]",
        "error": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "resolved_at": None,
    }

    with pytest.raises(TypeError, match="must decode to an object"):
        row_to_transition(row)


def test_prompt_renderer_rejects_missing_actionable_context() -> None:
    route = WebhookRoute(
        provider="test",
        name="events",
        workspace="team",
        secret_env="ENV",  # pragma: allowlist secret  # noqa: S106
        parse=lambda *_args: None,
    )
    event = WebhookEvent(
        delivery_id="delivery-1",
        event_type="Issue",
        action="update",
        subject_id="issue-1",
        occurred_at="2026-01-01T00:00:00+00:00",
        instructions=None,
        external_context=None,
        ignored_reason="ignored",
    )

    with pytest.raises(ValueError, match="lost its prompt context"):
        prompt_for_event(route, event)


async def test_screenshot_artifact_rejects_missing_provider_output(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="did not create"):
        await screenshot_artifact(tmp_path / "missing.png")


class _ReadyRuntime:
    name = "test-runtime"

    async def start(self, context: object) -> None:
        del context

    async def close(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True


async def test_connection_runtime_owner_closes_and_clears_owned_runtimes() -> None:
    owner = ConnectionRuntimeOwner()
    runtime = _ReadyRuntime()
    owner.set([runtime])

    assert owner.runtimes() == (runtime,)
    assert owner.status() == {"test-runtime": True}
    await owner.close()
    assert owner.runtimes() == ()
