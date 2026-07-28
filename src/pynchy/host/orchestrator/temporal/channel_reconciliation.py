"""Temporal activity for channel history reconciliation."""

from __future__ import annotations

from typing import Any, cast

from temporalio import activity

from pynchy.host.orchestrator.api import reconcile_all_channels
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)

CHANNEL_RECONCILIATION_ID = "channel-reconciliation"


@activity.defn(name="run_channel_reconciliation")
async def run_channel_reconciliation() -> str:
    """Run one channel reconciliation pass through the bound app deps."""
    try:
        await reconcile_all_channels(cast("Any", _require_scheduler_deps()))
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record activity failure.
        _record_activity_result(CHANNEL_RECONCILIATION_ID, "error", str(exc))
        raise
    _record_activity_result(CHANNEL_RECONCILIATION_ID, "completed")
    return "completed"
