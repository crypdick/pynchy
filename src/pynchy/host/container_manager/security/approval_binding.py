"""Exact-request binding checks for durable human approval decisions."""

from __future__ import annotations

import hmac
from typing import Any

from pynchy.host.container_manager.security.identity import payload_hash_matches


def approval_binding_error(
    pending: dict[str, Any],
    decision: dict[str, Any],
    *,
    request_id: str,
    source_group: str,
) -> str | None:
    """Return a safe rejection reason when approval bindings do not match."""
    request_data = pending.get("request_data")
    if not isinstance(request_data, dict):
        return "Pending approval payload is not a JSON object"

    expected = _required_strings(
        pending,
        ("guarded_action_id", "request_payload_hash", "source_group"),
    )
    if expected is None:
        return "Pending approval lacks a complete payload binding"
    expected_action_id, expected_hash, expected_workspace = expected

    if not payload_hash_matches(request_data, expected_hash):
        return "Pending approval payload changed after review"

    reviewed = _required_strings(
        decision,
        ("guarded_action_id", "request_payload_hash", "source_group"),
    )
    if reviewed is None:
        return "Approval decision lacks the reviewed payload binding"
    decision_action_id, decision_hash, decision_workspace = reviewed

    comparisons = (
        (expected_action_id, request_id, "Guarded-action identity does not match request"),
        (expected_workspace, source_group, "Approval workspace does not match request"),
        (decision_action_id, expected_action_id, "Decision targets another guarded action"),
        (decision_hash, expected_hash, "Decision targets another payload"),
        (decision_workspace, expected_workspace, "Decision targets another workspace"),
    )
    for actual, expected_value, error in comparisons:
        if not hmac.compare_digest(actual, expected_value):
            return error
    return None


def _required_strings(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, ...] | None:
    values = tuple(data.get(key) for key in keys)
    if not all(isinstance(value, str) and value for value in values):
        return None
    return tuple(str(value) for value in values)
