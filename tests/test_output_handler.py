"""Tests for pynchy.output_handler — streamed output handling and trace broadcasting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.messaging.output_handler import _next_trace_id, broadcast_trace, handle_streamed_output

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps() -> MagicMock:
    deps = MagicMock()
    deps.broadcast_to_channels = AsyncMock()
    deps.emit = MagicMock()
    return deps


def _make_group(*, name: str = "test-group", is_god: bool = False) -> MagicMock:
    group = MagicMock()
    group.name = name
    group.is_god = is_god
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
        "new_session_id": None,
        "error": None,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


# ---------------------------------------------------------------------------
# _next_trace_id
# ---------------------------------------------------------------------------


class TestNextTraceId:
    def test_includes_prefix(self):
        tid = _next_trace_id("think")
        assert tid.startswith("think-")

    def test_monotonically_increasing(self):
        a = _next_trace_id("x")
        b = _next_trace_id("x")
        # Counter portion (after last dash) should increase
        assert a != b

    def test_different_prefixes(self):
        a = _next_trace_id("tool")
        b = _next_trace_id("sys")
        assert a.startswith("tool-")
        assert b.startswith("sys-")


# ---------------------------------------------------------------------------
# broadcast_trace
# ---------------------------------------------------------------------------


class TestBroadcastTrace:
    @pytest.mark.asyncio
    async def test_stores_and_broadcasts(self):
        deps = _make_deps()

        with patch(
            "pynchy.messaging.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ) as mock_store:
            await broadcast_trace(
                deps,
                "group@g.us",
                "thinking",
                {"thinking": "processing..."},
                "💭 thinking...",
                db_id_prefix="think",
                db_sender="thinking",
            )

            mock_store.assert_awaited_once()
            deps.broadcast_to_channels.assert_awaited_once_with("group@g.us", "💭 thinking...")
            deps.emit.assert_called_once()


# ---------------------------------------------------------------------------
# handle_streamed_output — per output type
# ---------------------------------------------------------------------------


class TestHandleStreamedOutput:
    @pytest.mark.asyncio
    async def test_thinking_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="thinking", thinking="hmm...")

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_tool_use_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="tool_use", tool_name="Bash", tool_input={"command": "ls"})

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False
        # Check that the channel text includes the tool preview
        channel_text = deps.broadcast_to_channels.call_args[0][1]
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

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_system_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="system", system_subtype="init", system_data={"foo": "bar"})

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        deps.broadcast_to_channels.assert_awaited()

    @pytest.mark.asyncio
    async def test_result_empty_text_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<internal>hidden</internal>")

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        # After stripping internal tags, text is empty — no visible result
        assert result is False

    @pytest.mark.asyncio
    async def test_host_tagged_result_stored_as_host(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<host>System rebooting</host>")

        with patch(
            "pynchy.messaging.output_handler.store_message_direct",
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

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "g@g.us", group, output)

        # Channel should get a cost/duration summary
        channel_text = deps.broadcast_to_channels.call_args[0][1]
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

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert "key" in channel_text

    @pytest.mark.asyncio
    async def test_result_with_mixed_internal_and_visible_text(self):
        """Internal tags stripped, visible text remains."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<internal>secret</internal>Hello visible!")

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert "visible" in channel_text
        assert "secret" not in channel_text

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

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "g@g.us", group, output)

        channel_text = deps.broadcast_to_channels.call_args[0][1]
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

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False
        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert "tool" in channel_text.lower()

    @pytest.mark.asyncio
    async def test_system_event_with_none_subtype(self):
        """System event with no subtype should show 'unknown'."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="system", system_subtype=None, system_data=None)

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False
        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert "unknown" in channel_text

    @pytest.mark.asyncio
    async def test_host_channel_text_prefixed_with_house(self):
        """Host messages should be prefixed with the house emoji on channels."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<host>Restarting</host>")

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "g@g.us", group, output)

        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert channel_text.startswith("🏠")
        assert "Restarting" in channel_text

    @pytest.mark.asyncio
    async def test_normal_result_uses_agent_name(self):
        """Normal (non-host) results should be prefixed with agent name."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="Hello!")

        with (
            patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock),
            patch("pynchy.messaging.output_handler.get_settings") as mock_settings,
        ):
            mock_settings.return_value.agent.name = "TestBot"
            await handle_streamed_output(deps, "g@g.us", group, output)

        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert "TestBot" in channel_text

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
            "pynchy.messaging.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ) as mock_store:
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        # Should have stored both metadata and result
        assert mock_store.await_count >= 2
        # Should have broadcast both stats and the result text
        assert deps.broadcast_to_channels.await_count >= 2

    @pytest.mark.asyncio
    async def test_broadcast_trace_emits_correct_event_type(self):
        """broadcast_trace should emit an AgentTraceEvent with correct trace_type."""
        deps = _make_deps()

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await broadcast_trace(
                deps,
                "g@g.us",
                "tool_use",
                {"tool_name": "Bash"},
                "🔧 Bash: ls",
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
            "pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock
        ) as mock_store:
            await broadcast_trace(
                deps,
                "g@g.us",
                "system",
                {"subtype": "init"},
                "⚙️ system: init",
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
        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "plan@g.us", group, tool_use_output)

        # Then: send the tool_result
        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-plan",
            tool_result_content=plan_content,
            tool_result_is_error=False,
        )
        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "plan@g.us", group, tool_result_output)

        assert result is False
        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert "ExitPlanMode" in channel_text
        assert plan_content in channel_text

    @pytest.mark.asyncio
    async def test_normal_tool_result_still_generic(self):
        """tool_result for a normal tool should still show generic placeholder."""
        deps = _make_deps()
        group = _make_group()

        # First: send a tool_use for Bash
        tool_use_output = _make_output(
            type="tool_use", tool_name="Bash", tool_input={"command": "ls"}
        )
        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "normal@g.us", group, tool_use_output)

        # Then: send the tool_result
        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-bash",
            tool_result_content="file1.txt\nfile2.txt",
            tool_result_is_error=False,
        )
        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "normal@g.us", group, tool_result_output)

        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert channel_text == "📋 tool result"

    @pytest.mark.asyncio
    async def test_verbose_tool_result_with_empty_content_stays_generic(self):
        """ExitPlanMode with empty tool_result content falls back to generic."""
        deps = _make_deps()
        group = _make_group()

        tool_use_output = _make_output(type="tool_use", tool_name="ExitPlanMode", tool_input={})
        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "empty@g.us", group, tool_use_output)

        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-plan",
            tool_result_content="",
            tool_result_is_error=False,
        )
        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "empty@g.us", group, tool_result_output)

        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert channel_text == "📋 tool result"
