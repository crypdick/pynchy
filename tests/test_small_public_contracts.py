"""Focused tests for small public parsing and projection contracts."""

from __future__ import annotations

import pytest

import pynchy.plugins
from pynchy.host.orchestrator.job_gates import parse_wake_agent_gate
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
