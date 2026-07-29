"""Focused tests for small public parsing and projection contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

import pynchy.plugins
from pynchy.host.orchestrator.connection_runtime_owner import ConnectionRuntimeOwner
from pynchy.host.orchestrator.job_gates import parse_wake_agent_gate
from pynchy.host.orchestrator.webhook_event_rendering import prompt_for_event
from pynchy.plugins.api import WebhookEvent, WebhookRoute, load_connection_runtimes
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
from pynchy.plugins.integrations.linear_planning_tasks import admit_planning_issue
from pynchy.plugins.integrations.linear_self_echoes import linear_self_echo_recorder
from pynchy.plugins.integrations.linear_work_item_tasks import DecisionIssue
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


def test_plugin_namespace_rejects_unknown_attributes() -> None:
    with pytest.raises(AttributeError, match="no attribute"):
        _ = pynchy.plugins.not_a_plugin


def test_connection_runtime_loader_ignores_empty_plugin_contributions() -> None:
    plugin_manager = Mock()
    plugin_manager.hook.pynchy_connection_runtime.return_value = [None]

    assert load_connection_runtimes(plugin_manager) == []


@pytest.mark.asyncio
async def test_linear_conversation_resolution_requires_configuration(monkeypatch) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_conversation_identity._runtime.runtime", None
    )

    with pytest.raises(RuntimeError, match="Linear conversation runtime has not been configured"):
        await resolve_linear_issue_conversation("issue-1", "workspace", "account")


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
