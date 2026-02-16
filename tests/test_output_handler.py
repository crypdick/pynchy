"""Tests for pynchy.output_handler — streamed output handling and trace broadcasting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.output_handler import _next_trace_id, broadcast_trace, handle_streamed_output

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
            "pynchy.output_handler.store_message_direct",
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

        with patch("pynchy.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_tool_use_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="tool_use", tool_name="Bash", tool_input={"command": "ls"})

        with patch("pynchy.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_system_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="system", system_subtype="init", system_data={"foo": "bar"})

        with patch("pynchy.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        deps.broadcast_to_channels.assert_awaited()

    @pytest.mark.asyncio
    async def test_result_empty_text_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<internal>hidden</internal>")

        with patch("pynchy.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        # After stripping internal tags, text is empty — no visible result
        assert result is False

    @pytest.mark.asyncio
    async def test_host_tagged_result_stored_as_host(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<host>System rebooting</host>")

        with patch(
            "pynchy.output_handler.store_message_direct",
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

        with patch("pynchy.output_handler.store_message_direct", new_callable=AsyncMock):
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
