"""Focused tests for small public parsing and projection contracts."""

from __future__ import annotations

import pytest

import pynchy.plugins
from pynchy.host.orchestrator.connection_runtime_owner import ConnectionRuntimeOwner
from pynchy.host.orchestrator.job_gates import parse_wake_agent_gate
from pynchy.host.orchestrator.webhook_event_rendering import prompt_for_event
from pynchy.plugins.api import WebhookEvent, WebhookRoute
from pynchy.plugins.computer_use.artifacts import screenshot_artifact
from pynchy.state.work_item_rows import row_to_transition


def test_wake_gate_ignores_payloads_without_a_wake_decision() -> None:
    assert parse_wake_agent_gate('{"status": "ok"}') is None


def test_wake_gate_rejects_a_non_json_final_line() -> None:
    assert parse_wake_agent_gate('{"wakeAgent": true}\nnot-json') is None


def test_plugin_namespace_rejects_unknown_attributes() -> None:
    with pytest.raises(AttributeError, match="no attribute"):
        _ = pynchy.plugins.not_a_plugin


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
