"""Behavioral tests for consecutive tool-trace message coalescing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.host.orchestrator.messaging import router, sender, streaming
from pynchy.host.orchestrator.messaging.sender import (
    UpdatingMessage,
    deliver_updating_event,
)
from pynchy.host.orchestrator.messaging.streaming import OutputDeps, TraceBatcher
from pynchy.plugins.api import (
    Channel,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.state.outbound import OutboundDeliveryOperation
from pynchy.workspace.api import WorkspaceProfile


def _event(content: str) -> OutboundEvent:
    return OutboundEvent(type=OutboundEventType.TEXT, content=content)


def _channel(name: str, *, update_capable: bool = True) -> MagicMock:
    channel = MagicMock(spec=Channel)
    channel.name = name
    channel.is_connected.return_value = True
    channel.owns_jid.return_value = True
    channel.send_event = AsyncMock()
    if update_capable:
        channel.post_event = AsyncMock(return_value=f"{name}-message")
        channel.update_event = AsyncMock()
    return channel


def _deps(*channels: MagicMock) -> MagicMock:
    deps = MagicMock(spec=OutputDeps)
    deps.agent_name = "Pynchy"
    deps.channels = list(channels)
    deps.workspaces = {}
    deps.broadcast_to_channels = AsyncMock()
    deps.emit = MagicMock()
    return deps


@pytest.mark.asyncio
async def test_consecutive_tool_batches_update_one_remote_message() -> None:
    channel = _channel("discord")
    batcher = TraceBatcher(_deps(channel), cooldown=999.0)

    for content in ("🔧 first", "📋 first result", "🔧 second", "📋 second result"):
        batcher.enqueue("discord:channel:1", _event(content))
        await batcher.flush("discord:channel:1")

    channel.post_event.assert_awaited_once()
    assert [call.args[1] for call in channel.update_event.await_args_list] == [
        "discord-message",
        "discord-message",
        "discord-message",
    ]
    assert channel.update_event.await_args.args[2].content == (
        "🔧 first\n📋 first result\n🔧 second\n📋 second result"
    )


@pytest.mark.asyncio
async def test_events_inside_one_cooldown_post_as_one_batch() -> None:
    channel = _channel("discord")
    batcher = TraceBatcher(_deps(channel), cooldown=999.0)

    batcher.enqueue("discord:channel:1", _event("🔧 first"))
    batcher.enqueue("discord:channel:1", _event("📋 result"))
    await batcher.flush("discord:channel:1")

    channel.post_event.assert_awaited_once()
    assert channel.post_event.await_args.args[1].content == "🔧 first\n📋 result"
    channel.update_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_ends_the_message_before_the_next_tool_run() -> None:
    channel = _channel("discord")
    channel.post_event.side_effect = ["message-1", "message-2"]
    batcher = TraceBatcher(_deps(channel), cooldown=999.0)

    batcher.enqueue("discord:channel:1", _event("🔧 first"))
    await batcher.flush("discord:channel:1")
    batcher.enqueue("discord:channel:1", _event("📋 result"))
    await batcher.close("discord:channel:1")
    batcher.enqueue("discord:channel:1", _event("🔧 later"))
    await batcher.flush("discord:channel:1")

    assert [call.args[1].content for call in channel.post_event.await_args_list] == [
        "🔧 first",
        "🔧 later",
    ]
    channel.update_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_only_channel_receives_each_delta_without_replayed_content() -> None:
    channel = _channel("send-only", update_capable=False)
    batcher = TraceBatcher(_deps(channel), cooldown=999.0)

    batcher.enqueue("discord:channel:1", _event("🔧 first"))
    await batcher.flush("discord:channel:1")
    batcher.enqueue("discord:channel:1", _event("📋 result"))
    await batcher.flush("discord:channel:1")

    assert [call.args[1].content for call in channel.send_event.await_args_list] == [
        "🔧 first",
        "📋 result",
    ]


@pytest.mark.asyncio
async def test_update_failure_posts_new_accumulated_message_and_keeps_updating() -> None:
    channel = _channel("discord")
    channel.post_event.side_effect = ["message-1", "message-2"]
    channel.update_event.side_effect = [OSError("edit failed"), None]
    batcher = TraceBatcher(_deps(channel), cooldown=999.0)

    for content in ("🔧 first", "📋 result", "🔧 later"):
        batcher.enqueue("discord:channel:1", _event(content))
        await batcher.flush("discord:channel:1")

    assert [call.args[1].content for call in channel.post_event.await_args_list] == [
        "🔧 first",
        "🔧 first\n📋 result",
    ]
    assert [call.args[1] for call in channel.update_event.await_args_list] == [
        "message-1",
        "message-2",
    ]
    assert channel.update_event.await_args.args[2].content == "🔧 first\n📋 result\n🔧 later"


@pytest.mark.asyncio
async def test_update_failure_is_recorded_as_a_fallback_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel("discord")
    channel.post_event = AsyncMock(return_value="message-2")
    channel.update_event = AsyncMock(side_effect=OSError("edit failed"))
    record = AsyncMock(return_value=42)
    mark = AsyncMock()
    monkeypatch.setattr(sender.state, "record_outbound_deliveries", record)
    monkeypatch.setattr(sender.state, "mark_delivery_succeeded", mark)

    messages = await deliver_updating_event(
        _deps(channel),
        "discord:channel:1",
        _event("📋 result"),
        {
            "discord": UpdatingMessage(
                message_id="message-1",
                content="🔧 first",
            )
        },
        source="agent_trace",
    )

    delivery = record.await_args.args[3][0]
    assert delivery.operation is OutboundDeliveryOperation.EDIT
    assert delivery.remote_message_id == "message-1"
    assert mark.await_args.args[2:] == (
        OutboundDeliveryOperation.FALLBACK_POST,
        "message-2",
    )
    assert messages["discord"] == UpdatingMessage(
        message_id="message-2",
        content="🔧 first\n📋 result",
    )


@pytest.mark.asyncio
async def test_delivery_continues_when_outbound_ledger_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel("discord")
    record = AsyncMock(side_effect=RuntimeError("ledger unavailable"))
    mark = AsyncMock()
    monkeypatch.setattr(sender.state, "record_outbound_deliveries", record)
    monkeypatch.setattr(sender.state, "mark_delivery_succeeded", mark)

    messages = await deliver_updating_event(
        _deps(channel),
        "discord:channel:1",
        _event("📋 result"),
        {},
        source="agent_trace",
    )

    channel.post_event.assert_awaited_once()
    assert messages["discord"] == UpdatingMessage(
        message_id="discord-message",
        content="📋 result",
    )
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_chats_never_share_remote_message_ids() -> None:
    channel = _channel("discord")

    def post(jid: str, _event: OutboundEvent) -> str:
        return f"message-{jid.rsplit(':', maxsplit=1)[-1]}"

    channel.post_event.side_effect = post
    batcher = TraceBatcher(_deps(channel), cooldown=999.0)

    for jid in ("discord:channel:1", "discord:channel:2"):
        batcher.enqueue(jid, _event(f"🔧 first {jid}"))
        await batcher.flush(jid)
    for jid in ("discord:channel:1", "discord:channel:2"):
        batcher.enqueue(jid, _event(f"📋 result {jid}"))
        await batcher.flush(jid)

    assert [(call.args[0], call.args[1]) for call in channel.update_event.await_args_list] == [
        ("discord:channel:1", "message-1"),
        ("discord:channel:2", "message-2"),
    ]


@pytest.mark.asyncio
async def test_final_result_boundary_makes_later_tools_post_a_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel("discord")
    channel.post_event.side_effect = ["tool-message-1", "tool-message-2"]
    deps = _deps(channel)
    group = WorkspaceProfile(
        jid="discord:channel:1",
        name="test",
        folder="test",
        trigger="@pynchy",
    )
    monkeypatch.setattr(router, "store_message_direct", AsyncMock())
    streaming.init_trace_batcher(deps, cooldown=999.0)

    try:
        await router.handle_streamed_output(
            deps,
            group.jid,
            group,
            ContainerOutput(
                type="tool_use",
                status="success",
                tool_name="Bash",
                tool_input={"command": "true"},
            ),
        )
        batcher = streaming.get_trace_batcher()
        assert batcher is not None
        await batcher.flush(group.jid)
        await router.handle_streamed_output(
            deps,
            group.jid,
            group,
            ContainerOutput(
                type="tool_result",
                status="success",
                tool_result_id="tool-1",
                tool_result_content="",
            ),
        )
        await batcher.flush(group.jid)

        await router.handle_streamed_output(
            deps,
            group.jid,
            group,
            ContainerOutput(type="result", status="success", result="Done"),
        )
        await router.handle_streamed_output(
            deps,
            group.jid,
            group,
            ContainerOutput(
                type="tool_use",
                status="success",
                tool_name="Read",
                tool_input={"path": "next"},
            ),
        )
        await batcher.flush(group.jid)
    finally:
        streaming.reset_trace_batcher()

    assert [call.args[1].content for call in channel.post_event.await_args_list] == [
        "🔧 Bash:\n```\ntrue\n```",
        "🔧 Read",
    ]
    channel.update_event.assert_awaited_once()
    assert any(call.args[1].content == "Done" for call in channel.send_event.await_args_list)


@pytest.mark.asyncio
async def test_assistant_text_boundary_makes_later_tools_post_a_new_message() -> None:
    channel = _channel("discord")
    channel.post_event.side_effect = [
        "tool-message-1",
        "text-message",
        "tool-message-2",
    ]
    deps = _deps(channel)
    group = WorkspaceProfile(
        jid="discord:channel:1",
        name="test",
        folder="test",
        trigger="@pynchy",
    )
    streaming.init_trace_batcher(deps, cooldown=999.0)

    try:
        await router.handle_streamed_output(
            deps,
            group.jid,
            group,
            ContainerOutput(
                type="tool_use",
                status="success",
                tool_name="Read",
                tool_input={"path": "first"},
            ),
        )
        batcher = streaming.get_trace_batcher()
        assert batcher is not None
        await batcher.flush(group.jid)
        await router.handle_streamed_output(
            deps,
            group.jid,
            group,
            ContainerOutput(
                type="text",
                status="success",
                text="assistant text",
            ),
        )
        await router.handle_streamed_output(
            deps,
            group.jid,
            group,
            ContainerOutput(
                type="tool_use",
                status="success",
                tool_name="Read",
                tool_input={"path": "second"},
            ),
        )
        await batcher.flush(group.jid)
    finally:
        streaming.reset_trace_batcher()

    assert [call.args[1].content for call in channel.post_event.await_args_list] == [
        "🔧 Read",
        "assistant text",
        "🔧 Read",
    ]
    assert channel.update_event.await_args.args[1] == "text-message"
