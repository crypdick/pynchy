"""Temporal schedule time normalization contracts."""

from __future__ import annotations

from datetime import UTC

from pynchy.host.orchestrator.temporal.schedules import once_due_at


def test_once_due_at_without_value_uses_utc_now() -> None:
    due_at = once_due_at(None)

    assert due_at.tzinfo is UTC


def test_once_due_at_assumes_utc_for_naive_iso_timestamp() -> None:
    due_at = once_due_at("2026-07-29T12:00:00")

    assert due_at.isoformat() == "2026-07-29T12:00:00+00:00"
