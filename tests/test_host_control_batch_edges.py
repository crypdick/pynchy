"""Public host-control batch behavior at the message boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.orchestrator.messaging.host_controls import (
    execute_deferred_host_controls,
    intercept_immediate_checkpoint_controls,
    reclassify_batch_host_controls,
)
from tests.test_message_handler import _make_deps, _make_group, _make_message


@pytest.mark.asyncio
async def test_immediate_host_only_control_does_not_enqueue_more_input() -> None:
    deps = _make_deps()
    group = _make_group()
    pending = _make_message("pause", sender="system_notice")

    with (
        patch(
            "pynchy.host.orchestrator.messaging.sender_policy.get_messages_since",
            new_callable=AsyncMock,
            return_value=[pending],
        ),
        patch(
            "pynchy.host.orchestrator.messaging.host_controls.intercept_special_command",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await intercept_immediate_checkpoint_controls(deps, "g@g.us", group, [pending])

    assert result is True
    deps.queue.enqueue_message_check.assert_not_called()


@pytest.mark.asyncio
async def test_batch_reclassification_counts_handled_controls_only() -> None:
    deps = _make_deps()
    group = _make_group()
    messages = [_make_message("!status"), _make_message("!status", message_id="two")]

    with (
        patch(
            "pynchy.host.orchestrator.messaging.host_controls.host_control_kind",
            return_value=(True, False),
        ),
        patch(
            "pynchy.host.orchestrator.messaging.host_controls.reclassify_host_control",
            new_callable=AsyncMock,
            side_effect=[True, False],
        ),
    ):
        handled = await reclassify_batch_host_controls(
            deps, "g@g.us", group, messages, defer_lifecycle=False
        )

    assert handled == 1


@pytest.mark.asyncio
async def test_deferred_batch_executes_only_deferred_controls() -> None:
    deps = _make_deps()
    group = _make_group()
    ordinary = _make_message("normal")
    deferred = _make_message(
        "pause",
        message_id="deferred",
        metadata={"deferred_host_control": True},
    )

    with patch(
        "pynchy.host.orchestrator.messaging.host_controls.intercept_special_command",
        new_callable=AsyncMock,
    ) as intercept:
        await execute_deferred_host_controls(deps, "g@g.us", group, [ordinary, deferred])

    intercept.assert_awaited_once_with(deps, "g@g.us", group, deferred)
