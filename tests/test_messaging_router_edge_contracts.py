"""Public message-router behavior at trace and delivery boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.host.orchestrator.messaging import router
from pynchy.host.orchestrator.messaging.router import (
    broadcast_agent_input,
    handle_streamed_output,
)
from pynchy.host.orchestrator.messaging.streaming import OutputDeps
from pynchy.plugins.api import Channel
from pynchy.workspace.api import WorkspaceProfile


def _deps(*, with_channel: bool = False) -> MagicMock:
    deps = MagicMock(spec=OutputDeps)
    deps.agent_name = "Pynchy"
    deps.broadcast_to_channels = AsyncMock()
    deps.emit = MagicMock()
    if with_channel:
        channel = MagicMock(spec=Channel)
        channel.name = "test"
        channel.is_connected.return_value = True
        channel.owns_jid.return_value = True
        channel.send_event = AsyncMock()
        deps.channels = [channel]
    else:
        deps.channels = []
    return deps


def _group() -> MagicMock:
    group = MagicMock(spec=WorkspaceProfile)
    group.name = "test-group"
    group.folder = "test-group"
    return group


@pytest.mark.asyncio
async def test_user_input_trace_emits_mapping_messages() -> None:
    deps = _deps()
    messages = [{"content": "hello"}]

    await broadcast_agent_input(deps, "chat:1", messages)

    assert deps.emit.call_count == 1
    assert deps.emit.call_args.args[0].data["content"] == "hello"


@pytest.mark.asyncio
async def test_synthetic_input_truncates_long_content() -> None:
    deps = _deps()
    messages = [{"content": "x" * 600}]

    await broadcast_agent_input(deps, "chat:1", messages, source="scheduled_task")

    event = deps.broadcast_to_channels.await_args.args[1]
    assert len(event.content) < 600
    assert event.content.endswith("...")
    assert deps.emit.call_count == 1


@pytest.mark.asyncio
async def test_empty_thinking_trace_uses_a_visible_fallback() -> None:
    deps = _deps()

    await handle_streamed_output(
        deps,
        "chat:1",
        _group(),
        ContainerOutput(type="thinking", status="success", thinking=None),
    )

    assert "thinking..." in deps.broadcast_to_channels.await_args.args[1].content


@pytest.mark.asyncio
async def test_verbose_tool_result_is_truncated_for_channel_delivery() -> None:
    deps = _deps()
    group = _group()
    await handle_streamed_output(
        deps,
        "chat:1",
        group,
        ContainerOutput(
            type="tool_use",
            status="success",
            tool_name="ExitPlanMode",
            tool_input={},
        ),
    )
    await handle_streamed_output(
        deps,
        "chat:1",
        group,
        ContainerOutput(
            type="tool_result",
            status="success",
            tool_result_content="x" * 5000,
        ),
    )

    event = deps.broadcast_to_channels.await_args.args[1]
    assert "chars omitted" in event.content
    assert len(event.content) < 5000


@pytest.mark.asyncio
async def test_empty_formatted_result_is_not_stored_or_delivered() -> None:
    deps = _deps(with_channel=True)

    with patch.object(router, "store_message_direct", new_callable=AsyncMock) as store:
        result = await handle_streamed_output(
            deps,
            "chat:1",
            _group(),
            ContainerOutput(type="result", status="success", result="<internal> </internal>"),
        )

    assert result is False
    store.assert_not_awaited()
    deps.channels[0].send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_delivery_ignores_missing_work_item_ledger() -> None:
    deps = _deps(with_channel=True)

    with (
        patch.object(router, "store_message_direct", new_callable=AsyncMock),
        patch.object(
            router,
            "mark_work_item_delivery_delivered_for_turn",
            new_callable=AsyncMock,
            side_effect=RuntimeError("ledger unavailable"),
        ),
    ):
        result = await handle_streamed_output(
            deps,
            "chat:1",
            _group(),
            ContainerOutput(type="result", status="success", result="done"),
            turn_id="turn-1",
        )

    assert result is True
    deps.channels[0].send_event.assert_awaited_once()
