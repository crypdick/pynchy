"""Tests for pynchy.output_handler — streamed output handling and trace broadcasting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.chat._streaming import stream_states
from pynchy.chat.output_handler import (
    _next_trace_id,
    broadcast_trace,
    handle_streamed_output,
    init_trace_batcher,
)

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
    ch.send_message = AsyncMock()
    deps.channels = [ch]
    deps.get_channel_jid = MagicMock(return_value=None)
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
            "pynchy.chat.output_handler.store_message_direct",
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_tool_use_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="tool_use", tool_name="Bash", tool_input={"command": "ls"})

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_system_event_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="system", system_subtype="init", system_data={"foo": "bar"})

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        # Result finalization goes through the bus (finalize_stream_or_broadcast)
        # which calls ch.send_message on the mock channel.
        deps._test_channel.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_result_empty_text_returns_false(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<internal>hidden</internal>")

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        # After stripping internal tags, text is empty — no visible result
        assert result is False

    @pytest.mark.asyncio
    async def test_host_tagged_result_stored_as_host(self):
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<host>System rebooting</host>")

        with patch(
            "pynchy.chat.output_handler.store_message_direct",
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        channel_text = deps._test_channel.send_message.call_args[0][1]
        assert "key" in channel_text

    @pytest.mark.asyncio
    async def test_result_with_mixed_internal_and_visible_text(self):
        """Internal tags stripped, visible text remains."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="<internal>secret</internal>Hello visible!")

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        channel_text = deps._test_channel.send_message.call_args[0][1]
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "g@g.us", group, output)

        channel_text = deps._test_channel.send_message.call_args[0][1]
        assert channel_text.startswith("🏠")
        assert "Restarting" in channel_text

    @pytest.mark.asyncio
    async def test_normal_result_uses_lobster_prefix(self):
        """Normal (non-host) results should be prefixed with 🦞."""
        deps = _make_deps()
        group = _make_group()
        output = _make_output(type="result", result="Hello!")

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "g@g.us", group, output)

        channel_text = deps._test_channel.send_message.call_args[0][1]
        assert channel_text.startswith("🦞")
        assert "Hello!" in channel_text

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
            "pynchy.chat.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ) as mock_store:
            result = await handle_streamed_output(deps, "g@g.us", group, output)

        assert result is True
        # Should have stored both metadata and result
        assert mock_store.await_count >= 2
        # Metadata stats go through deps.broadcast_to_channels (trace path)
        assert deps.broadcast_to_channels.await_count >= 1
        # Result text goes through the bus (finalize_stream_or_broadcast → ch.send_message)
        deps._test_channel.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_broadcast_trace_emits_correct_event_type(self):
        """broadcast_trace should emit an AgentTraceEvent with correct trace_type."""
        deps = _make_deps()

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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
            "pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock
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
        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "plan@g.us", group, tool_use_output)

        # Then: send the tool_result
        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-plan",
            tool_result_content=plan_content,
            tool_result_is_error=False,
        )
        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
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
        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "normal@g.us", group, tool_use_output)

        # Then: send the tool_result
        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-bash",
            tool_result_content="file1.txt\nfile2.txt",
            tool_result_is_error=False,
        )
        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "normal@g.us", group, tool_result_output)

        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert channel_text == "📋 tool result"

    @pytest.mark.asyncio
    async def test_verbose_tool_result_with_empty_content_stays_generic(self):
        """ExitPlanMode with empty tool_result content falls back to generic."""
        deps = _make_deps()
        group = _make_group()

        tool_use_output = _make_output(type="tool_use", tool_name="ExitPlanMode", tool_input={})
        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "empty@g.us", group, tool_use_output)

        tool_result_output = _make_output(
            type="tool_result",
            tool_result_id="t-plan",
            tool_result_content="",
            tool_result_is_error=False,
        )
        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, "empty@g.us", group, tool_result_output)

        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert channel_text == "📋 tool result"

    # -----------------------------------------------------------------------
    # Stream interleaving — text and tool traces in chronological order
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_tool_use_finalizes_active_text_stream(self):
        """When a tool_use event arrives, any in-progress text stream should be
        finalized (marked as done) so it becomes its own message before the
        tool trace appears."""
        deps = _make_deps()
        group = _make_group()
        jid = "interleave@g.us"

        ch = MagicMock()
        ch.name = "test"
        ch.is_connected.return_value = True
        ch.post_message = AsyncMock(return_value="msg-1")
        ch.update_message = AsyncMock()
        ch.send_message = AsyncMock()
        ch.owns_jid = MagicMock(return_value=True)
        deps.channels = [ch]
        deps.get_channel_jid = MagicMock(return_value=None)

        # Initialize the trace batcher so it captures tool traces
        init_trace_batcher(deps, cooldown=30)

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            # 1. Stream some text
            await handle_streamed_output(
                deps, jid, group, _make_output(type="text", text="Let me check")
            )
            # There should be an active stream state
            assert jid in stream_states

            # 2. tool_use arrives — should finalize text stream
            await handle_streamed_output(
                deps,
                jid,
                group,
                _make_output(type="tool_use", tool_name="Bash", tool_input={"cmd": "ls"}),
            )
            # Stream state should have been cleaned up
            assert jid not in stream_states
            # Text should have been finalized (update_message called with final=True
            # means no trailing cursor block character)
            final_text = ch.update_message.call_args[0][2]
            assert "Let me check" in final_text
            assert "\u258c" not in final_text  # No cursor character

    @pytest.mark.asyncio
    async def test_new_text_after_tool_flushes_batcher(self):
        """When text starts after a tool cycle, any buffered traces should be
        flushed first so tools appear before the new text."""
        deps = _make_deps()
        group = _make_group()
        jid = "flush@g.us"

        ch = MagicMock()
        ch.name = "test"
        ch.is_connected.return_value = True
        ch.post_message = AsyncMock(return_value="msg-1")
        ch.update_message = AsyncMock()
        ch.send_message = AsyncMock()
        ch.owns_jid = MagicMock(return_value=True)
        deps.channels = [ch]
        deps.get_channel_jid = MagicMock(return_value=None)

        # Initialize the trace batcher with a long cooldown so it doesn't
        # auto-flush during the test.
        init_trace_batcher(deps, cooldown=999)

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            # 1. Send a tool_use (enqueued in batcher, not yet broadcast)
            await handle_streamed_output(
                deps,
                jid,
                group,
                _make_output(type="tool_use", tool_name="Read", tool_input={}),
            )
            # Trace is in the batcher, not yet broadcast
            assert deps.broadcast_to_channels.await_count == 0

            # 2. Send a tool_result (also enqueued)
            await handle_streamed_output(
                deps,
                jid,
                group,
                _make_output(
                    type="tool_result",
                    tool_result_id="t-1",
                    tool_result_content="ok",
                ),
            )
            # Still buffered in the batcher
            assert deps.broadcast_to_channels.await_count == 0

            # 3. New text arrives — should trigger batcher flush first
            await handle_streamed_output(
                deps, jid, group, _make_output(type="text", text="Here is the result")
            )
            # The batcher should have been flushed — traces broadcast
            assert deps.broadcast_to_channels.await_count >= 1
            flushed = deps.broadcast_to_channels.call_args_list[0][0][1]
            assert "Read" in flushed or "tool result" in flushed

        # Clean up stream state
        stream_states.pop(jid, None)

    @pytest.mark.asyncio
    async def test_full_interleaving_sequence(self):
        """End-to-end test: text -> tool_use -> tool_result -> text -> result
        produces properly ordered channel messages."""
        deps = _make_deps()
        group = _make_group()
        jid = "full@g.us"

        # Track all outbound actions in order
        actions: list[tuple[str, str]] = []  # (action_type, content_snippet)

        ch = MagicMock()
        ch.name = "test"
        ch.is_connected.return_value = True

        async def _post(target_jid, text):
            actions.append(("post", text[:60]))
            return f"msg-{len(actions)}"

        async def _update(target_jid, msg_id, text):
            actions.append(("update", text[:60]))

        async def _send(target_jid, text):
            actions.append(("send", text[:60]))

        ch.post_message = AsyncMock(side_effect=_post)
        ch.update_message = AsyncMock(side_effect=_update)
        ch.send_message = AsyncMock(side_effect=_send)
        ch.owns_jid = MagicMock(return_value=True)
        deps.channels = [ch]
        deps.get_channel_jid = MagicMock(return_value=None)

        # Use a long cooldown so the batcher only flushes when we force it
        init_trace_batcher(deps, cooldown=999)

        async def _broadcast(chat_jid, text, **kw):
            actions.append(("broadcast", text[:60]))

        deps.broadcast_to_channels = AsyncMock(side_effect=_broadcast)

        with patch("pynchy.chat.output_handler.store_message_direct", new_callable=AsyncMock):
            # Step 1: text "I'll check the file"
            await handle_streamed_output(
                deps, jid, group, _make_output(type="text", text="I'll check the file")
            )

            # Step 2: tool_use Read — should finalize text stream
            await handle_streamed_output(
                deps,
                jid,
                group,
                _make_output(type="tool_use", tool_name="Read", tool_input={"path": "/tmp/x"}),
            )

            # Step 3: tool_result
            await handle_streamed_output(
                deps,
                jid,
                group,
                _make_output(
                    type="tool_result",
                    tool_result_id="t-1",
                    tool_result_content="file contents here",
                ),
            )

            # Step 4: new text "The file contains" — should flush batcher first
            await handle_streamed_output(
                deps, jid, group, _make_output(type="text", text="The file contains")
            )

            # Step 5: result
            await handle_streamed_output(
                deps, jid, group, _make_output(type="result", result="The file contains X")
            )

        # Verify ordering: text finalized -> tool traces -> new text -> result
        # Filter to just the semantically meaningful actions
        post_actions = [a for a in actions if a[0] == "post"]
        assert len(post_actions) >= 2, f"Expected at least 2 posts, got {actions}"

        # The first post should be the initial text
        assert "check the file" in post_actions[0][1]

        # There should be trace broadcasts (tool_use + tool_result) between
        # the two text posts
        broadcast_actions = [a for a in actions if a[0] == "broadcast"]
        assert any("Read" in b[1] or "tool result" in b[1] for b in broadcast_actions)

        # Clean up
        stream_states.pop(jid, None)
