"""Boundary coverage for scheduled-work IPC handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.container_manager.ipc.deps import IpcDeps
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.identity import ReceiptVerification
from pynchy.state import get_all_host_jobs

pytest_plugins = ("tests.ipc_auth_support",)


def _schedule_request() -> dict[str, object]:
    return {
        "type": "schedule_host_job",
        "name": "edge-job",
        "command": "echo edge",
        "schedule_type": "cron",
        "schedule_value": "0 9 * * *",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("timeout_seconds", "600"), ("cwd", 42), ("memory", "yes")],
    ids=["non-integer-timeout", "non-string-cwd", "non-boolean-memory"],
)
async def test_schedule_host_job_rejects_malformed_optional_fields(deps, field, value) -> None:
    request = _schedule_request()
    request[field] = value

    await dispatch(request, "admin-1", True, deps)

    assert await get_all_host_jobs() == []


@pytest.mark.parametrize(
    ("schedule_type", "schedule_value"),
    [("interval", "0"), ("cron", "not a cron expression")],
    ids=["non-positive-interval", "invalid-cron"],
)
async def test_schedule_host_job_rejects_invalid_schedule_values(
    deps,
    schedule_type: str,
    schedule_value: str,
) -> None:
    request = _schedule_request()
    request["schedule_type"] = schedule_type
    request["schedule_value"] = schedule_value

    await dispatch(request, "admin-1", True, deps)

    assert await get_all_host_jobs() == []


async def test_schedule_host_job_accepts_positive_interval(deps) -> None:
    request = _schedule_request()
    request["schedule_type"] = "interval"
    request["schedule_value"] = "60"

    await dispatch(request, "admin-1", True, deps)

    jobs = await get_all_host_jobs()
    assert len(jobs) == 1
    assert jobs[0].schedule_type == "interval"
    assert jobs[0].schedule_value == "60"


async def test_invalid_schedule_receipt_stops_before_persistence(deps) -> None:
    with patch(
        "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
        new_callable=AsyncMock,
        return_value=ReceiptVerification.INVALID,
    ) as verify:
        await dispatch(_schedule_request(), "admin-1", True, deps)

    verify.assert_awaited_once()
    assert await get_all_host_jobs() == []


async def test_schedule_host_job_requires_scheduled_work_persistence() -> None:
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=ReceiptVerification.VALID,
        ),
        pytest.raises(TypeError, match="scheduled-work persistence"),
    ):
        await dispatch(_schedule_request(), "admin-1", True, MagicMock(spec=IpcDeps))


async def test_missing_host_job_action_is_a_noop(deps) -> None:
    await dispatch(
        {"type": "pause_task", "taskId": "host-does-not-exist"},
        "admin-1",
        True,
        deps,
    )
