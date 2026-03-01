"""Tests for pynchy.output_handler — streamed output handling and trace broadcasting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.orchestrator.messaging import streaming
from pynchy.host.orchestrator.messaging.router import (
    _last_result_ids,
    _next_trace_id,
    broadcast_trace,
    handle_streamed_output,
    init_trace_batcher,
)
from pynchy.host.orchestrator.messaging.streaming import StreamState, stream_states


@pytest.fixture(autouse=True)
def _clean_trace_batcher():
    """Ensure the global trace batcher is cleared before each test."""
    streaming._trace_batcher = None
    yield
    streaming._trace_batcher = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps() -> MagicMock:
    deps = MagicMock()
    deps.broadcast_to_channels = AsyncMock()
    deps.emit = MagicMock()
    # Provide a mock channel so finalize_stream_or_broadcast (bus) can work.
    # The bus iterates deps.channels directly for result finalization.
    ch = MagicMock()
    ch.name = "test"
    ch.is_connected.return_value = True
    ch.send_event = AsyncMock()
    deps.channels = [ch]
    deps._test_channel = ch  # Expose for test assertions
    return deps


def _make_group(*, name: str = "test-group", is_admin: bool = False) -> MagicMock:
    group = MagicMock()
    group.name = name
    group.is_admin = is_admin
    return group


def _make_output(**overrides) -> MagicMock:
    """Create a ContainerOutput-like object with sensible defaults."""
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
        "status": "ok",
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _get_broadcast_content(deps: MagicMock) -> str:
    """Extract the content from the last broadcast_to_channels call.

    The second positional arg is now an OutboundEvent; return its .content.
    """
    event = deps.broadcast_to_channels.call_args[0][1]
    return event.content


def _get_send_event(deps: MagicMock):
    """Extract the OutboundEvent from the last send_event call on the test channel."""
    return deps._test_channel.send_event.call_args[0][1]


# ---------------------------------------------------------------------------
# _next_trace_id
# ---------------------------------------------------------------------------


class TestNextTraceId:
    def test_prefix_is_present(self):
        tid = _next_trace_id("tool")
        assert tid.startswith("tool-")

    def test_ids_are_unique(self):
        a = _next_trace_id("t")
        b = _next_trace_id("t")
        assert a != b


# ---------------------------------------------------------------------------
# init_trace_batcher
# ---------------------------------------------------------------------------


class TestInitTraceBatcher:
    def test_init_creates_batcher(self):
        deps = _make_deps()
        init_trace_batcher(deps, cooldown=999)
        from pynchy.host.orchestrator.messaging import streaming

        batcher = streaming.get_trace_batcher()
        assert batcher is not None
        # Clean up global state so subsequent tests get direct broadcast
        streaming._trace_batcher = None


# ---------------------------------------------------------------------------
# handle_streamed_output
# ---------------------------------------------------------------------------


class TestHandleStreamedOutput:
    @pytest.mark.asyncio
    async def test_thinking_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="thinking", thinking="hmm...")

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_tool_use_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="tool_use", tool_name="Bash", tool_input={"command": "ls"})

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_system_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="system", system_subtype="init", system_data={"foo": "bar"})

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        # Result finalization goes through the bus (finalize_stream_or_broadcast)
        # which calls ch.send_event on the mock channel.
        deps._test_channel.send_event.assert_awaited()

    @pytest.mark.asyncio
    async def test_result_internal_only_still_sends(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<internal>hidden</internal>")

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        # Internal-only text is formatted (not stripped), so it's still visible
        assert result is True

    @pytest.mark.asyncio
    async def test_host_tagged_result_stored_as_host(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<host>System rebooting</host>")

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct",
            new_callable=AsyncMock,
        ) as mock_store:
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        call_kwargs = mock_store.call_args_list[-1][1]
        assert call_kwargs["sender"] == "host"
        assert call_kwargs["message_type"] == "host"
        assert call_kwargs["content"] == "System rebooting"

    @pytest.mark.asyncio
    async def test_result_metadata_cost_formatting(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(
            type="result",
            result=None,
            result_metadata={"total_cost_usd": 0.05, "duration_ms": 12345, "num_turns": 3},
        )

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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
        output = _make_output(type="result", result={"key": "value"})

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False
        channel_text = _get_broadcast_content(deps)
        assert "unknown" in channel_text

    @pytest.mark.asyncio
    async def test_host_channel_text_prefixed_with_house(self):
        """Host messages should use HOST event type."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<host>Restarting</host>")

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            await handle_streamed_output(deps, "g@g.us", group, output)

        from pynchy.types import OutboundEventType

        event = _get_send_event(deps)
        assert event.type == OutboundEventType.HOST
        assert "Restarting" in event.content

    @pytest.mark.asyncio
    async def test_normal_result_uses_lobster_prefix(self):
        """Normal (non-host) results should use RESULT event type."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="Hello!")

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            await handle_streamed_output(deps, "g@g.us", group, output)

        from pynchy.types import OutboundEventType

        event = _get_send_event(deps)
        assert event.type == OutboundEventType.RESULT
        assert "Hello!" in event.content

    @pytest.mark.asyncio
    async def test_result_and_metadata_both_processed(self):
        """When both result text and metadata exist, both should be processed."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(
            type="result",
            result="Done!",
            result_metadata={"total_cost_usd": 0.03, "duration_ms": 5000},
        )

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct",
            new_callable=AsyncMock,
        ) as mock_store:
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        # Should have stored both metadata and result
        assert mock_store.await_count >= 2
        # Metadata stats go through deps.broadcast_to_channels (trace path)
        assert deps.broadcast_to_channels.await_count >= 1
        # Result text goes through the bus (finalize_stream_or_broadcast -> ch.send_event)
        deps._test_channel.send_event.assert_awaited()

    @pytest.mark.asyncio
    async def test_broadcast_trace_emits_correct_event_type(self):
        """broadcast_trace should emit an AgentTraceEvent with correct trace_type."""
        deps = _make_deps()

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            await broadcast_trace(
                deps,
                "g@g.us",
                "tool_use",
                {"tool_name": "Bash"},
                "\U0001f527 Bash: ls",
                db_id_prefix="tool",
                db_sender="tool_use",
            )

        event = deps.emit.call_args[0][0]
        assert event.trace_type == "tool_use"
        assert event.chat_jid == "g@g.us"
        assert event.data == {"tool_name": "Bash"}

    @pytest.mark.asyncio
    async def test_broadcast_trace_uses_custom_message_type(self):
        """broadcast_trace should use the specified message_type for DB storage."""
        deps = _make_deps()

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ) as mock_store:
            await broadcast_trace(
                deps,
                "g@g.us",
                "system",
                {"subtype": "init"},
                "\u2699\ufe0f system: init",
                db_id_prefix="sys",
                db_sender="system",
                message_type="system",
            )

        call_kwargs = mock_store.call_args[1]
        assert call_kwargs["message_type"] == "system"

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
        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            await handle_streamed_output(deps, "plan@g.us", group, tool_use_output)

        # Then: send the tool_result
        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-plan",
            tool_result_content=plan_content,
            tool_result_is_error=False,
        )
        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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
        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            await handle_streamed_output(deps, "g@g.us", group, tool_use_output)

        # Then: tool_result for Bash
        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-bash",
            tool_result_content="output",
            tool_result_is_error=False,
        )
        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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
        state = StreamState()
        state.buffer = "Hello"
        state.message_ids = {"test": "msg-123"}
        stream_states["g@g.us"] = state

        output = _make_output(type="result", result="Final answer")

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        # The stashed IDs should match the stream state
        assert _last_result_ids.get("g@g.us") == {"test": "msg-123"}
        # Clean up
        _last_result_ids.pop("g@g.us", None)

    @pytest.mark.asyncio
    async def test_full_interleaving_sequence(self):
        """Simulate a full think -> tool -> result sequence and verify ordering."""
        deps = _make_deps()
        group = _make_group()

        with patch(
            "pynchy.host.orchestrator.messaging.router.store_message_direct", new_callable=AsyncMock
        ):
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
