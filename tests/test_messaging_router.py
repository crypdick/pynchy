"""Tests for pynchy.output_handler — streamed output handling and trace broadcasting."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.host.orchestrator.messaging import router, streaming
from pynchy.host.orchestrator.messaging.router import (
    broadcast_agent_input,
    broadcast_trace,
    handle_streamed_output,
    init_trace_batcher,
    pop_last_result_ids,
)
from pynchy.host.orchestrator.messaging.streaming import OutputDeps, StreamState, stream_states
from pynchy.plugins.api import (
    Channel,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.workspace.api import WorkspaceProfile


@pytest.fixture(autouse=True)
def _clean_trace_batcher():
    """Ensure the global trace batcher is cleared before each test."""
    streaming.reset_trace_batcher()
    yield
    streaming.reset_trace_batcher()


@pytest.fixture(autouse=True)
def _mock_store_message_direct(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    store = AsyncMock()
    monkeypatch.setattr(router, "store_message_direct", store)
    return store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps() -> MagicMock:
    deps = MagicMock(spec=OutputDeps)
    deps.agent_name = "Pynchy"
    deps.workspaces = {}
    deps.broadcast_to_channels = AsyncMock()
    deps.emit = MagicMock()
    # Final results use the channel bus directly.
    # The bus iterates deps.channels directly for result finalization.
    ch = MagicMock(spec=Channel)
    ch.name = "test"
    ch.is_connected.return_value = True
    ch.send_event = AsyncMock()
    deps.channels = [ch]
    deps._test_channel = ch  # Expose for test assertions
    return deps


def _make_group(*, name: str = "test-group", is_admin: bool = False) -> MagicMock:
    group = MagicMock(spec=WorkspaceProfile)
    group.name = name
    group.folder = name
    group.is_admin = is_admin
    return group


def _make_output(**overrides) -> ContainerOutput:
    """Create a ContainerOutput with sensible defaults."""
    defaults = {
        "type": "result",
        "result": None,
        "result_metadata": None,
        "thinking": None,
        "tool_name": None,
        "tool_input": None,
        "tool_result_id": None,
        "tool_result_content": None,
        "tool_result_is_error": None,
        "system_subtype": None,
        "system_data": None,
        "text": None,
        "status": "success",
    }
    defaults.update(overrides)
    return ContainerOutput(**defaults)


def _get_broadcast_content(deps: MagicMock) -> str:
    """Extract the content from the last broadcast_to_channels call.

    The second positional arg is now an OutboundEvent; return its .content.
    """
    event = deps.broadcast_to_channels.call_args[0][1]
    return event.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "hidden_learning_review",
        "hidden_plan_review",
        "external:hidden_plan_review",
    ],
)
async def test_hidden_review_input_is_not_broadcast_or_traced(source: str):
    deps = _make_deps()
    messages = [{"role": "user", "content": "hidden reviewer prompt"}]

    await broadcast_agent_input(
        deps,
        "learning-review:default",
        messages,
        source=source,
    )

    deps.broadcast_to_channels.assert_not_awaited()
    deps.emit.assert_not_called()


@pytest.mark.asyncio
async def test_trusted_provider_input_uses_a_readable_source_label():
    deps = _make_deps()
    messages = [{"role": "user", "content": "A new comment was posted."}]

    await broadcast_agent_input(
        deps,
        "discord:channel:linear-thread",
        messages,
        source="trusted:linear",
    )

    assert _get_broadcast_content(deps) == "» [Linear] A new comment was posted."
    assert deps.emit.call_args.args[0].data["source"] == "trusted:linear"


def _get_send_event(deps: MagicMock):
    """Extract the OutboundEvent from the last send_event call on the test channel."""
    return deps._test_channel.send_event.call_args[0][1]


# ---------------------------------------------------------------------------
# trace broadcast
# ---------------------------------------------------------------------------


class TestBroadcastTrace:
    @pytest.mark.asyncio
    async def test_broadcast_trace_does_not_persist_trace_body(self):
        deps = _make_deps()
        await broadcast_trace(deps, "g@g.us", "tool_use", {"tool_name": "Bash"}, "text")

        cast("AsyncMock", router.store_message_direct).assert_not_awaited()
        deps.emit.assert_called_once()


# ---------------------------------------------------------------------------
# init_trace_batcher
# ---------------------------------------------------------------------------


class TestInitTraceBatcher:
    def test_init_creates_batcher(self):
        deps = _make_deps()
        init_trace_batcher(deps, cooldown=999.0)

        batcher = streaming.get_trace_batcher()
        assert batcher is not None
        # Clean up global state so subsequent tests get direct broadcast
        streaming.reset_trace_batcher()


# ---------------------------------------------------------------------------
# streaming helpers
# ---------------------------------------------------------------------------


class TestStreamTextToChannels:
    @pytest.mark.asyncio
    async def test_posts_first_stream_message_and_tracks_message_id(self):
        deps = _make_deps()
        deps._test_channel.owns_jid.return_value = True
        deps._test_channel.post_event = AsyncMock(return_value="msg-123")
        deps._test_channel.update_event = AsyncMock()
        state = StreamState(event=OutboundEvent(type=OutboundEventType.TEXT, content="hello"))

        with patch(
            "pynchy.host.orchestrator.messaging.streaming.time.monotonic",
            return_value=10.0,
        ):
            await streaming.stream_text_to_channels(deps, "g@g.us", state)

        deps._test_channel.post_event.assert_awaited_once_with("g@g.us", state.event)
        deps._test_channel.update_event.assert_not_awaited()
        assert state.message_ids == {"test": "msg-123"}
        assert state.event.metadata["cursor"] is True

    @pytest.mark.asyncio
    async def test_updates_existing_stream_message_on_final_flush(self):
        deps = _make_deps()
        deps._test_channel.owns_jid.return_value = True
        deps._test_channel.post_event = AsyncMock()
        deps._test_channel.update_event = AsyncMock()
        state = StreamState(event=OutboundEvent(type=OutboundEventType.TEXT, content="hello"))
        state.message_ids["test"] = "msg-123"

        with patch(
            "pynchy.host.orchestrator.messaging.streaming.time.monotonic",
            return_value=10.0,
        ):
            await streaming.stream_text_to_channels(deps, "g@g.us", state, final=True)

        deps._test_channel.post_event.assert_not_awaited()
        deps._test_channel.update_event.assert_awaited_once_with("g@g.us", "msg-123", state.event)
        assert state.event.metadata["cursor"] is False


# ---------------------------------------------------------------------------
# handle_streamed_output
# ---------------------------------------------------------------------------


class TestHandleStreamedOutput:
    @pytest.mark.asyncio
    async def test_empty_text_delta_does_not_start_a_stream(self):
        deps = _make_deps()
        deps._test_channel.owns_jid.return_value = True
        deps._test_channel.post_event = AsyncMock()
        group = _make_group()

        try:
            result = await handle_streamed_output(
                deps, "g@g.us", group, _make_output(type="text", text="")
            )
        finally:
            stream_states.pop("g@g.us", None)

        assert result is False
        deps._test_channel.post_event.assert_not_awaited()
        deps.emit.assert_called_once()

    @pytest.mark.asyncio
    async def test_text_deltas_append_to_one_stream_message(self):
        deps = _make_deps()
        deps._test_channel.owns_jid.return_value = True
        deps._test_channel.post_event = AsyncMock(return_value="message-1")
        deps._test_channel.update_event = AsyncMock()
        group = _make_group()

        try:
            with patch(
                "pynchy.host.orchestrator.messaging.streaming.time.monotonic",
                side_effect=[1.0, 2.0],
            ):
                await handle_streamed_output(
                    deps, "g@g.us", group, _make_output(type="text", text="first")
                )
                await handle_streamed_output(
                    deps, "g@g.us", group, _make_output(type="text", text=" second")
                )
        finally:
            stream_states.pop("g@g.us", None)

        deps._test_channel.post_event.assert_awaited_once()
        deps._test_channel.update_event.assert_awaited_once()
        assert deps._test_channel.update_event.await_args.args[2].content == "first second"

    @pytest.mark.asyncio
    async def test_result_metadata_without_displayable_fields_is_trace_only(self):
        deps = _make_deps()
        group = _make_group()

        result = await handle_streamed_output(
            deps,
            "g@g.us",
            group,
            _make_output(
                type="result",
                result_metadata={
                    "total_cost_usd": None,
                    "duration_ms": None,
                    "num_turns": None,
                },
            ),
        )

        assert result is False
        deps.broadcast_to_channels.assert_not_awaited()
        deps.emit.assert_called_once()

    @pytest.mark.asyncio
    async def test_thinking_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="thinking", thinking="hmm...")

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_tool_use_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="tool_use", tool_name="Bash", tool_input={"command": "ls"})

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False
        # Check that the channel text includes the tool preview
        channel_text = _get_broadcast_content(deps)
        assert "Bash" in channel_text

    @pytest.mark.asyncio
    async def test_tool_result_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(
            type="tool_result",
            tool_result_id="t-1",
            tool_result_content="ok",
            tool_result_is_error=False,
        )

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_system_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="system", system_subtype="init", system_data={"foo": "bar"})

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_text_event_emits_trace_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="text", text="partial text")

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False
        deps.emit.assert_called_once()
        event = deps.emit.call_args[0][0]
        assert event.trace_type == "text"

    @pytest.mark.asyncio
    async def test_result_with_text_returns_true(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="Hello user!")

        with patch.object(
            router,
            "mark_work_item_delivery_delivered_for_turn",
            new_callable=AsyncMock,
        ) as mark_delivered:
            result = await handle_streamed_output(
                deps,
                "g@g.us",
                group,
                output,
                turn_id="turn_1",
            )

        assert result is True
        mark_delivered.assert_awaited_once_with("turn_1")
        store = cast("AsyncMock", router.store_message_direct)
        store.assert_awaited_once()
        saved = store.await_args.kwargs
        assert saved["metadata"]["turn_id"] == "turn_1"
        assert saved["chat_jid"] == "g@g.us"
        assert saved["sender"] == "bot"
        assert saved["message_type"] == "assistant"
        assert saved["content"] == "Hello user!"
        assert saved["metadata"]["source"] == "agent_result"
        assert saved["metadata"]["workspace_name"] == group.name
        # Result finalization goes through the channel bus.
        # which calls ch.send_event on the mock channel.
        deps._test_channel.send_event.assert_awaited()

    @pytest.mark.asyncio
    async def test_failed_channel_send_does_not_confirm_requester_delivery(self):
        deps = _make_deps()
        deps._test_channel.send_event.side_effect = OSError("channel unavailable")
        group = _make_group()
        output = _make_output(type="result", result="Blocked on credentials.")

        with patch.object(
            router,
            "mark_work_item_delivery_delivered_for_turn",
            new_callable=AsyncMock,
        ) as mark_delivered:
            result = await handle_streamed_output(
                deps,
                "g@g.us",
                group,
                output,
                turn_id="turn-blocked",
            )

        assert result is True
        deps._test_channel.send_event.assert_awaited_once()
        mark_delivered.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_result_internal_only_still_sends(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<internal>hidden</internal>")

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        # Internal-only text is formatted (not stripped), so it's still visible
        assert result is True

    @pytest.mark.asyncio
    async def test_host_tagged_result_stored_as_host(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<host>System rebooting</host>")

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        saved = cast("AsyncMock", router.store_message_direct).await_args.kwargs
        assert saved["sender"] == "host"
        assert saved["message_type"] == "host"
        assert saved["content"] == "System rebooting"

    @pytest.mark.asyncio
    async def test_result_metadata_cost_formatting(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(
            type="result",
            result=None,
            result_metadata={"total_cost_usd": 0.05, "duration_ms": 12345, "num_turns": 3},
        )

        await handle_streamed_output(deps, "g@g.us", group, output)

        # Channel should get a cost/duration summary
        channel_text = _get_broadcast_content(deps)
        assert "0.05 USD" in channel_text
        assert "12.3s" in channel_text
        assert "3 turns" in channel_text

    @pytest.mark.asyncio
    async def test_result_no_result_no_metadata_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result=None, result_metadata=None)

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    # -----------------------------------------------------------------------
    # Additional edge cases
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_result_with_dict_result_serialized_to_json(self):
        """Non-string results should be JSON-serialized before processing."""
        deps = _make_deps()
        group = _make_group()
        with pytest.warns(UserWarning, match="violates type hint"):
            output = _make_output(type="result", result={"key": "value"})

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        event = _get_send_event(deps)
        assert "key" in event.content

    @pytest.mark.asyncio
    async def test_result_with_mixed_internal_and_visible_text(self):
        """Internal tags formatted as brain emoji, visible text remains."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<internal>thinking</internal>Hello visible!")

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        event = _get_send_event(deps)
        assert "visible" in event.content

    @pytest.mark.asyncio
    async def test_result_metadata_partial_fields(self):
        """Metadata with only some fields still formats correctly."""
        deps = _make_deps()
        group = _make_group()
        # Only cost, no duration or turns
        output = _make_output(
            type="result",
            result=None,
            result_metadata={"total_cost_usd": 0.12},
        )

        await handle_streamed_output(deps, "g@g.us", group, output)

        channel_text = _get_broadcast_content(deps)
        assert "0.12 USD" in channel_text
        # Duration and turns should NOT be in the output
        assert "s" not in channel_text.replace("USD", "")  # no seconds
        assert "turns" not in channel_text

    @pytest.mark.asyncio
    async def test_result_metadata_empty_dict_no_broadcast(self):
        """Empty metadata dict should not produce a stats broadcast."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result=None, result_metadata={})

        await handle_streamed_output(deps, "g@g.us", group, output)

        # Store is called for metadata, but no channel broadcast for empty parts
        # The broadcast should NOT be called because parts list is empty
        deps.broadcast_to_channels.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tool_use_with_none_tool_name_defaults(self):
        """tool_use with None tool_name should default to 'tool'."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="tool_use", tool_name=None, tool_input=None)

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False
        channel_text = _get_broadcast_content(deps)
        assert "tool" in channel_text.lower()

    @pytest.mark.asyncio
    async def test_system_event_with_none_subtype(self):
        """System event with no subtype should show 'unknown'."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="system", system_subtype=None, system_data=None)

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False
        channel_text = _get_broadcast_content(deps)
        assert "unknown" in channel_text

    @pytest.mark.asyncio
    async def test_codex_thread_started_system_event_is_trace_only(self):
        """Codex thread lifecycle events should not leak into channels."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(
            type="system",
            system_subtype="thread.started",
            system_data={"session_id": "codex:gpt-5.2-codex:thread-1"},
        )

        await handle_streamed_output(deps, "g@g.us", group, output)

        deps.broadcast_to_channels.assert_not_awaited()
        event = deps.emit.call_args[0][0]
        assert event.trace_type == "system"
        assert event.data == {
            "subtype": "thread.started",
            "data": {"session_id": "codex:gpt-5.2-codex:thread-1"},
        }

    @pytest.mark.asyncio
    async def test_host_channel_text_prefixed_with_house(self):
        """Host messages should use HOST event type."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<host>Restarting</host>")

        await handle_streamed_output(deps, "g@g.us", group, output)

        event = _get_send_event(deps)
        assert event.type == OutboundEventType.HOST
        assert "Restarting" in event.content

    @pytest.mark.asyncio
    async def test_normal_result_uses_lobster_prefix(self):
        """Normal (non-host) results should use RESULT event type."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="Hello!")

        await handle_streamed_output(deps, "g@g.us", group, output)

        event = _get_send_event(deps)
        assert event.type == OutboundEventType.RESULT
        assert "Hello!" in event.content

    @pytest.mark.asyncio
    async def test_result_and_metadata_both_processed(self):
        """Metadata is broadcast live while only the final result is stored."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(
            type="result",
            result="Done!",
            result_metadata={"total_cost_usd": 0.03, "duration_ms": 5000},
        )

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        # Only the final result is stored as conversation content; metadata is live trace data.
        assert cast("AsyncMock", router.store_message_direct).await_count == 1
        # Metadata stats go through deps.broadcast_to_channels (trace path)
        assert deps.broadcast_to_channels.await_count >= 1
        # Result text reaches the channel through the bus.
        deps._test_channel.send_event.assert_awaited()

    @pytest.mark.asyncio
    async def test_broadcast_trace_emits_correct_event_type(self):
        """broadcast_trace should emit an AgentTraceEvent with correct trace_type."""
        deps = _make_deps()

        await broadcast_trace(deps, "g@g.us", "tool_use", {"tool_name": "Bash"}, "Bash: ls")

        event = deps.emit.call_args[0][0]
        assert event.trace_type == "tool_use"
        assert event.chat_jid == "g@g.us"
        assert event.data == {"tool_name": "Bash"}

    @pytest.mark.asyncio
    async def test_broadcast_trace_broadcasts_channel_text(self):
        """broadcast_trace should still send trace text to live channels."""
        deps = _make_deps()

        await broadcast_trace(deps, "g@g.us", "system", {"subtype": "init"}, "system: init")

        event = deps.broadcast_to_channels.await_args.args[1]
        assert event.content == "system: init"

    # -----------------------------------------------------------------------
    # Verbose tool result (ExitPlanMode, EnterPlanMode)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_exit_plan_mode_tool_result_broadcasts_full_content(self):
        """tool_result following ExitPlanMode should broadcast full content."""
        deps = _make_deps()
        group = _make_group()
        plan_content = "## Plan\n1. Do thing A\n2. Do thing B\n3. Verify"

        # First: send the tool_use for ExitPlanMode
        tool_use_output = _make_output(type="tool_use", tool_name="ExitPlanMode", tool_input={})
        await handle_streamed_output(deps, "plan@g.us", group, tool_use_output)

        # Then: send the tool_result
        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-plan",
            tool_result_content=plan_content,
            tool_result_is_error=False,
        )
        result = await handle_streamed_output(deps, "plan@g.us", group, tool_result_output)

        assert result is False
        channel_text = _get_broadcast_content(deps)
        assert "ExitPlanMode" in channel_text
        assert plan_content in channel_text

    @pytest.mark.asyncio
    async def test_normal_tool_result_still_generic(self):
        """tool_result for a normal tool should still show generic placeholder."""
        deps = _make_deps()
        group = _make_group()

        # First: send a tool_use for Bash
        tool_use_output = _make_output(type="tool_use", tool_name="Bash", tool_input={})
        await handle_streamed_output(deps, "g@g.us", group, tool_use_output)

        # Then: tool_result for Bash
        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-bash",
            tool_result_content="output",
            tool_result_is_error=False,
        )
        result = await handle_streamed_output(deps, "g@g.us", group, tool_result_output)

        assert result is False
        channel_text = _get_broadcast_content(deps)
        assert "tool result" in channel_text.lower()

    # -----------------------------------------------------------------------
    # Stream finalization + _last_result_ids
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_final_result_stashes_outbound_ids(self):
        """After a streamed result, _last_result_ids should contain the stream IDs."""
        deps = _make_deps()
        group = _make_group()

        # Pre-populate a stream state with message IDs
        event = OutboundEvent(type=OutboundEventType.TEXT, content="Hello")
        state = StreamState(event=event)
        state.message_ids = {"test": "msg-123"}
        stream_states["g@g.us"] = state

        output = _make_output(type="result", result="Final answer")

        result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        # The stashed IDs should match the stream state (pop also clears them)
        assert pop_last_result_ids("g@g.us") == {"test": "msg-123"}

    @pytest.mark.asyncio
    async def test_full_interleaving_sequence(self):
        """Simulate a full think -> tool -> result sequence and verify ordering."""
        deps = _make_deps()
        group = _make_group()

        r1 = await handle_streamed_output(
            deps, "g@g.us", group, _make_output(type="thinking", thinking="planning...")
        )
        r2 = await handle_streamed_output(
            deps,
            "g@g.us",
            group,
            _make_output(type="tool_use", tool_name="Bash", tool_input={"command": "date"}),
        )
        r3 = await handle_streamed_output(
            deps,
            "g@g.us",
            group,
            _make_output(type="tool_result", tool_result_content="output", tool_result_id="t1"),
        )
        r4 = await handle_streamed_output(
            deps, "g@g.us", group, _make_output(type="result", result="Done!")
        )

        assert r1 is False
        assert r2 is False
        assert r3 is False
        assert r4 is True
