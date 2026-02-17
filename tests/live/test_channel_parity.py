"""Channel parity tests — verify output consistency across TUI, WhatsApp, and Slack.

These tests synthesize various message types (agent results, host messages, tool
traces, system events, etc.), push them through the common broadcasting code
paths, and verify that all channels receive equivalent output.

"Equivalent" accounts for known, intentional differences:
- Slack omits the assistant name prefix (the platform shows bot identity)
- WhatsApp/TUI prefix agent messages with the assistant name
- Streaming channels (Slack) receive updates via post_message/update_message

Run with:
    uv run pytest tests/live/ -m "live and parity"
    uv run pytest tests/live/ -m live
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.adapters import HostMessageBroadcaster, MessageBroadcaster
from pynchy.messaging.channel_handler import (
    broadcast_to_channels,
    send_reaction_to_channels,
    set_typing_on_channels,
)
from pynchy.messaging.output_handler import (
    broadcast_agent_input,
    handle_streamed_output,
)
from pynchy.messaging.router import format_outbound
from pynchy.types import ContainerOutput, RegisteredGroup

from .conftest import (
    RecordingChannel,
    make_slack_channel,
    make_tui_channel,
    make_whatsapp_channel,
)

pytestmark = [pytest.mark.live, pytest.mark.parity]

CHAT_JID = "group@g.us"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_prefix(text: str) -> str:
    """Strip the emoji prefix if present, for comparing content parity.

    This normalizes '🦞 Hello world' → 'Hello world' so we can compare
    the actual content across channels that differ in prefix behavior.
    """
    prefix = "🦞 "
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


def _normalize_messages(channel: RecordingChannel) -> list[str]:
    """Get normalized message texts from a channel for parity comparison.

    Strips the emoji prefix (since that's a known channel-specific
    difference), so the underlying content can be compared directly.
    """
    return [_strip_prefix(text) for _, text in channel.sent_messages]


def _make_deps(channels: list[RecordingChannel]) -> Any:
    """Create a mock ChannelDeps with the given channels."""
    from unittest.mock import MagicMock

    deps = MagicMock()
    deps.channels = channels
    deps.event_bus = MagicMock()
    deps.get_channel_jid = MagicMock(return_value=None)
    return deps


# ---------------------------------------------------------------------------
# 1. broadcast_to_channels — raw text parity
# ---------------------------------------------------------------------------


class TestBroadcastRawTextParity:
    """Verify that broadcast_to_channels sends identical text to all channels."""

    @pytest.fixture
    def channels(self) -> list[RecordingChannel]:
        return [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]

    async def test_simple_text_reaches_all_channels(self, channels):
        """Plain text broadcasts should arrive identically at every channel."""
        deps = _make_deps(channels)
        await broadcast_to_channels(deps, CHAT_JID, "Hello, world!")

        for ch in channels:
            texts = ch.get_texts(CHAT_JID)
            assert texts == ["Hello, world!"], f"{ch.name} got {texts}"

    async def test_emoji_prefix_messages_identical(self, channels):
        """Host and system messages with emoji prefixes should be identical."""
        deps = _make_deps(channels)

        messages = [
            "🏠 Context cleared. Starting fresh session.",
            "🔧 Bash: git status",
            "📋 tool result",
            "💭 thinking...",
            "⚙️ system: init",
            "📊 0.42 USD · 3.2s · 5 turns",
            "» [Scheduled Task] Run daily report",
        ]

        for msg in messages:
            for ch in channels:
                ch.clear()
            await broadcast_to_channels(deps, CHAT_JID, msg)
            texts_by_channel = {ch.name: ch.get_texts(CHAT_JID) for ch in channels}

            # All channels should receive the same text
            reference = texts_by_channel[channels[0].name]
            for ch in channels:
                assert texts_by_channel[ch.name] == reference, (
                    f"Parity violation for '{msg}': "
                    f"{ch.name}={texts_by_channel[ch.name]} vs "
                    f"{channels[0].name}={reference}"
                )

    async def test_multiline_text_parity(self, channels):
        """Multi-line messages should be sent identically."""
        deps = _make_deps(channels)
        text = "Line 1\nLine 2\nLine 3\n\nLine 5 (after blank)"
        await broadcast_to_channels(deps, CHAT_JID, text)

        for ch in channels:
            assert ch.get_texts(CHAT_JID) == [text], f"{ch.name} multiline mismatch"

    async def test_unicode_and_special_chars_parity(self, channels):
        """Unicode, emoji, and special characters should pass through unchanged."""
        deps = _make_deps(channels)
        text = '🇺🇸 Héllo wörld! 你好 — "quotes" & <tags>'
        await broadcast_to_channels(deps, CHAT_JID, text)

        for ch in channels:
            assert ch.get_texts(CHAT_JID) == [text], f"{ch.name} unicode mismatch"

    async def test_empty_text_parity(self, channels):
        """Empty string broadcasts should arrive at all channels."""
        deps = _make_deps(channels)
        await broadcast_to_channels(deps, CHAT_JID, "")

        for ch in channels:
            assert ch.get_texts(CHAT_JID) == [""], f"{ch.name} empty text mismatch"

    async def test_long_text_parity(self, channels):
        """Very long messages should arrive identically (no truncation)."""
        deps = _make_deps(channels)
        text = "x" * 10000
        await broadcast_to_channels(deps, CHAT_JID, text)

        for ch in channels:
            assert ch.get_texts(CHAT_JID) == [text], f"{ch.name} long text mismatch"


# ---------------------------------------------------------------------------
# 2. format_outbound — per-channel formatting parity
# ---------------------------------------------------------------------------


class TestFormatOutboundParity:
    """Verify that format_outbound applies consistent prefix rules across channels."""

    def test_whatsapp_prefixes_assistant_name(self):
        ch = make_whatsapp_channel()
        result = format_outbound(ch, "Hello world")
        assert result == "🦞 Hello world"

    def test_slack_does_not_prefix(self):
        ch = make_slack_channel()
        result = format_outbound(ch, "Hello world")
        assert result == "Hello world"

    def test_tui_prefixes_assistant_name(self):
        ch = make_tui_channel()
        result = format_outbound(ch, "Hello world")
        assert result == "🦞 Hello world"

    def test_internal_tags_stripped_consistently(self):
        """<internal> tags should be stripped from ALL channels."""
        raw = "<internal>thinking about it</internal>The answer is 42"
        for ch in [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]:
            result = format_outbound(ch, raw)
            assert "<internal>" not in result, f"{ch.name} leaked internal tags"
            assert "The answer is 42" in result, f"{ch.name} lost content"

    def test_all_internal_returns_empty_for_all_channels(self):
        """If text is entirely <internal>, all channels should get empty string."""
        raw = "<internal>secret thoughts</internal>"
        for ch in [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]:
            result = format_outbound(ch, raw)
            assert result == "", f"{ch.name} returned non-empty for all-internal: {result!r}"

    def test_content_after_strip_is_identical(self):
        """After removing the known prefix difference, content should be identical."""
        raw = "Here is a complex response with **markdown** and `code`."
        results = {}
        for ch in [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]:
            result = format_outbound(ch, raw)
            results[ch.name] = _strip_prefix(result)

        # All normalized results should be the same
        values = list(results.values())
        for name, val in results.items():
            assert val == values[0], f"{name} content differs: {val!r} vs {values[0]!r}"


# ---------------------------------------------------------------------------
# 3. Host message parity
# ---------------------------------------------------------------------------


class TestHostMessageParity:
    """Verify host message broadcast reaches all channels with consistent formatting."""

    @staticmethod
    def _make_host_broadcaster(
        channels: list,
    ) -> HostMessageBroadcaster:
        """Create a HostMessageBroadcaster for test channels."""
        broadcaster = MessageBroadcaster(channels)
        return HostMessageBroadcaster(broadcaster, AsyncMock(), AsyncMock(), lambda _: None)

    async def test_host_message_emoji_prefix_consistent(self):
        """broadcast_host_message should prepend 🏠 for all channels."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        host_broadcaster = self._make_host_broadcaster(channels)

        await host_broadcaster.broadcast_host_message(CHAT_JID, "⚠️ Agent error occurred")

        for ch in channels:
            texts = ch.get_texts(CHAT_JID)
            assert len(texts) == 1, f"{ch.name} got {len(texts)} messages"
            assert "🏠" in texts[0], f"{ch.name} missing house emoji"
            assert "Agent error occurred" in texts[0], f"{ch.name} missing content"

    async def test_host_message_text_identical_across_channels(self):
        """All channels should receive the exact same host message text."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        host_broadcaster = self._make_host_broadcaster(channels)

        messages = [
            "Context cleared. Starting fresh session.",
            "Deploy complete — restarting containers.",
            "Session expired, reconnecting.",
            "Worktree updated to latest main.",
        ]

        for msg in messages:
            for ch in channels:
                ch.clear()
            await host_broadcaster.broadcast_host_message(CHAT_JID, msg)

            texts = {ch.name: ch.get_texts(CHAT_JID) for ch in channels}
            reference = texts[channels[0].name]
            for ch in channels:
                assert texts[ch.name] == reference, (
                    f"Host message parity violation for '{msg}': "
                    f"{ch.name}={texts[ch.name]} vs {channels[0].name}={reference}"
                )


# ---------------------------------------------------------------------------
# 4. Reaction and typing parity
# ---------------------------------------------------------------------------


class TestReactionAndTypingParity:
    """Verify reactions and typing indicators behave consistently."""

    async def test_typing_sent_to_channels_that_support_it(self):
        """set_typing should be called on channels with set_typing, skipped for others."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = _make_deps(channels)

        await set_typing_on_channels(deps, CHAT_JID, True)

        # All our recording channels have set_typing, so all should get it
        for ch in channels:
            assert len(ch.typing_states) == 1, f"{ch.name} typing not received"
            assert ch.typing_states[0] == (CHAT_JID, True)

    async def test_reaction_sent_to_channels_that_support_it(self):
        """send_reaction should be called on channels with send_reaction."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = _make_deps(channels)

        await send_reaction_to_channels(deps, CHAT_JID, "msg-1", "user@s", "👍")

        for ch in channels:
            assert len(ch.reactions) == 1, f"{ch.name} reaction not received"
            assert ch.reactions[0] == (CHAT_JID, "msg-1", "user@s", "👍")


# ---------------------------------------------------------------------------
# 5. Agent output (result) parity via handle_streamed_output
# ---------------------------------------------------------------------------


class TestAgentOutputParity:
    """Verify agent output messages reach all channels through handle_streamed_output."""

    def _make_output_deps(self, channels: list[RecordingChannel]) -> Any:
        """Create OutputDeps for handle_streamed_output."""
        from unittest.mock import MagicMock

        deps = MagicMock()
        deps.channels = channels

        async def mock_broadcast(jid, text, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_message(jid, text)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = MagicMock()
        return deps

    async def test_agent_text_result_parity(self):
        """Agent result text should reach all channels (with prefix differences)."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = self._make_output_deps(channels)
        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            result="The answer is 42.",
            type="result",
            new_session_id="s1",
        )

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            sent = await handle_streamed_output(deps, CHAT_JID, group, result)

        assert sent is True, "handle_streamed_output should return True for text result"

        # All channels should have received the message
        for ch in channels:
            assert len(ch.sent_messages) >= 1, f"{ch.name} received no messages"

        # Normalize and compare content
        normalized = {ch.name: _normalize_messages(ch) for ch in channels}
        ref = normalized[channels[0].name]
        for ch in channels:
            assert normalized[ch.name] == ref, (
                f"Agent output parity violation: "
                f"{ch.name}={normalized[ch.name]} vs {channels[0].name}={ref}"
            )

    async def test_host_tagged_result_parity(self):
        """<host>...</host> wrapped results should get 🏠 prefix on all channels."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = self._make_output_deps(channels)
        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            result="<host>Context cleared. Starting fresh session.</host>",
            type="result",
            new_session_id="s1",
        )

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            sent = await handle_streamed_output(deps, CHAT_JID, group, result)

        assert sent is True

        # All channels should receive house-emoji prefixed message
        for ch in channels:
            texts = ch.get_texts()
            assert any("🏠" in t for t in texts), f"{ch.name} missing 🏠 prefix for host message"
            assert any("Context cleared" in t for t in texts), (
                f"{ch.name} missing host message content"
            )

        # The actual text should be identical since host messages don't get
        # assistant name prefix — they use 🏠 prefix for all channels
        all_texts = [ch.get_texts() for ch in channels]
        for i, _ch in enumerate(channels):
            assert all_texts[i] == all_texts[0], (
                f"Host message text differs: {ch.name}={all_texts[i]} vs "
                f"{channels[0].name}={all_texts[0]}"
            )

    async def test_thinking_trace_parity(self):
        """Thinking trace events should broadcast consistently."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = self._make_output_deps(channels)
        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            type="thinking",
            thinking="Let me consider this carefully...",
        )

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            sent = await handle_streamed_output(deps, CHAT_JID, group, result)

        assert sent is False  # Thinking traces don't count as user-visible results

        # All channels should get the thinking indicator
        for ch in channels:
            texts = ch.get_texts()
            assert any("thinking" in t.lower() for t in texts), (
                f"{ch.name} missing thinking indicator"
            )

        # Text should be identical across channels
        all_texts = [ch.get_texts() for ch in channels]
        for i, _ch in enumerate(channels):
            assert all_texts[i] == all_texts[0], (
                f"Thinking parity violation: {ch.name}={all_texts[i]} vs "
                f"{channels[0].name}={all_texts[0]}"
            )

    async def test_tool_use_trace_parity(self):
        """Tool use trace events should broadcast consistently."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = self._make_output_deps(channels)
        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        tool_uses = [
            ("Bash", {"command": "git status"}),
            ("Read", {"file_path": "/src/pynchy/app.py"}),
            ("Edit", {"file_path": "/src/pynchy/router.py"}),
            ("Grep", {"pattern": "TODO", "path": "src/"}),
            ("WebFetch", {"url": "https://example.com"}),
            ("Task", {"description": "Explore codebase"}),
        ]

        for tool_name, tool_input in tool_uses:
            for ch in channels:
                ch.clear()

            result = ContainerOutput(
                status="success",
                type="tool_use",
                tool_name=tool_name,
                tool_input=tool_input,
            )

            with patch(
                "pynchy.messaging.output_handler.store_message_direct",
                new_callable=AsyncMock,
            ):
                await handle_streamed_output(deps, CHAT_JID, group, result)

            # All channels should get the tool preview
            for ch in channels:
                texts = ch.get_texts()
                assert len(texts) >= 1, f"{ch.name} received no messages for {tool_name}"
                assert any("🔧" in t for t in texts), (
                    f"{ch.name} missing wrench emoji for {tool_name}"
                )

            # Text should be identical across channels
            all_texts = [ch.get_texts() for ch in channels]
            for i, _ch in enumerate(channels):
                assert all_texts[i] == all_texts[0], (
                    f"Tool use parity violation for {tool_name}: "
                    f"{ch.name}={all_texts[i]} vs "
                    f"{channels[0].name}={all_texts[0]}"
                )

    async def test_tool_result_trace_parity(self):
        """Tool result trace events should broadcast consistently."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = self._make_output_deps(channels)
        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            type="tool_result",
            tool_result_id="tr-1",
            tool_result_content="command output here",
            tool_result_is_error=False,
        )

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, CHAT_JID, group, result)

        # All channels should get the tool result indicator
        for ch in channels:
            texts = ch.get_texts()
            assert any("📋" in t for t in texts), (
                f"{ch.name} missing clipboard emoji for tool_result"
            )

        # Text should be identical
        all_texts = [ch.get_texts() for ch in channels]
        for i, _ch in enumerate(channels):
            assert all_texts[i] == all_texts[0], (
                f"Tool result parity: {ch.name}={all_texts[i]} vs {channels[0].name}={all_texts[0]}"
            )

    async def test_system_event_parity(self):
        """System events (non-init) should broadcast consistently."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = self._make_output_deps(channels)
        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            type="system",
            system_subtype="context_reset",
            system_data={"reason": "manual"},
        )

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, CHAT_JID, group, result)

        # Non-init system events should reach channels
        for ch in channels:
            texts = ch.get_texts()
            assert any("⚙️" in t for t in texts), f"{ch.name} missing gear emoji for system event"

        all_texts = [ch.get_texts() for ch in channels]
        for i, _ch in enumerate(channels):
            assert all_texts[i] == all_texts[0], (
                f"System event parity: {ch.name}={all_texts[i]} vs "
                f"{channels[0].name}={all_texts[0]}"
            )

    async def test_system_init_suppressed_on_all_channels(self):
        """System init events should be suppressed from ALL channels equally."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = self._make_output_deps(channels)
        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            type="system",
            system_subtype="init",
            system_data={"session_id": "abc123"},
        )

        with patch("pynchy.messaging.output_handler.store_message_direct", new_callable=AsyncMock):
            await handle_streamed_output(deps, CHAT_JID, group, result)

        # Init should be suppressed from ALL channels
        for ch in channels:
            texts = ch.get_texts()
            assert len(texts) == 0, (
                f"{ch.name} received init event that should be suppressed: {texts}"
            )


# ---------------------------------------------------------------------------
# 6. Agent input broadcast parity
# ---------------------------------------------------------------------------


class TestAgentInputBroadcastParity:
    """Verify that synthetic agent inputs (scheduled tasks, handoffs) broadcast consistently."""

    async def test_scheduled_task_input_parity(self):
        """Scheduled task inputs should broadcast identically to all channels."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = _make_deps(channels)

        # Wire up broadcast_to_channels to actually call channels
        async def mock_broadcast(jid, text, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_message(jid, text)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        messages = [
            {"sender_name": "Scheduler", "content": "Run the daily health check."},
        ]

        await broadcast_agent_input(deps, CHAT_JID, messages, source="scheduled_task")

        for ch in channels:
            texts = ch.get_texts()
            assert len(texts) == 1, f"{ch.name} got {len(texts)} messages"
            assert "Scheduled Task" in texts[0], f"{ch.name} missing source label"
            assert "daily health check" in texts[0], f"{ch.name} missing content"

        all_texts = [ch.get_texts() for ch in channels]
        for i, _ch in enumerate(channels):
            assert all_texts[i] == all_texts[0], (
                f"Scheduled task input parity: {ch.name}={all_texts[i]}"
            )

    async def test_user_input_not_broadcast(self):
        """Normal user inputs should NOT be broadcast (they're already visible)."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = _make_deps(channels)
        deps.broadcast_to_channels = AsyncMock()
        deps.emit = lambda *a, **kw: None

        messages = [
            {"sender_name": "Alice", "content": "Hello agent"},
        ]

        await broadcast_agent_input(deps, CHAT_JID, messages, source="user")

        # No channels should receive user messages (they're already visible in chat)
        for ch in channels:
            assert len(ch.sent_messages) == 0, (
                f"{ch.name} received user input that shouldn't be broadcast"
            )

    async def test_reset_handoff_input_parity(self):
        """Context handoff inputs should broadcast identically."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = _make_deps(channels)

        async def mock_broadcast(jid, text, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_message(jid, text)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        messages = [
            {"sender_name": "System", "content": "Continuing from previous session..."},
        ]

        await broadcast_agent_input(deps, CHAT_JID, messages, source="reset_handoff")

        for ch in channels:
            texts = ch.get_texts()
            assert len(texts) == 1, f"{ch.name} got {len(texts)} messages"
            assert "Context Handoff" in texts[0]

        all_texts = [ch.get_texts() for ch in channels]
        for i, _ch in enumerate(channels):
            assert all_texts[i] == all_texts[0]


# ---------------------------------------------------------------------------
# 7. Full trace sequence parity (end-to-end)
# ---------------------------------------------------------------------------


class TestFullTraceSequenceParity:
    """Simulate a complete agent interaction and verify parity across the full sequence."""

    async def test_complete_agent_interaction_parity(self):
        """Run a complete thinking → tool_use → tool_result → result sequence
        and verify all channels receive equivalent output at each step."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]

        deps_mock = _make_deps(channels)

        async def mock_broadcast(jid, text, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_message(jid, text)

        deps_mock.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps_mock.emit = lambda *a, **kw: None

        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        trace_sequence = [
            ContainerOutput(status="success", type="thinking", thinking="Let me check..."),
            ContainerOutput(
                status="success",
                type="tool_use",
                tool_name="Bash",
                tool_input={"command": "git log --oneline -5"},
            ),
            ContainerOutput(
                status="success",
                type="tool_result",
                tool_result_id="tr-1",
                tool_result_content="abc123 Fix bug\ndef456 Add feature",
            ),
            ContainerOutput(
                status="success",
                type="tool_use",
                tool_name="Read",
                tool_input={"file_path": "/src/pynchy/app.py"},
            ),
            ContainerOutput(
                status="success",
                type="tool_result",
                tool_result_id="tr-2",
                tool_result_content="# App code here...",
            ),
            ContainerOutput(
                status="success",
                result="Recent changes: last 5 commits show a bug fix and a new feature.",
                type="result",
                new_session_id="s1",
            ),
        ]

        # Accumulate messages per-channel at each step
        message_counts = {ch.name: [] for ch in channels}

        with patch(
            "pynchy.messaging.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ):
            for trace in trace_sequence:
                await handle_streamed_output(deps_mock, CHAT_JID, group, trace)
                for ch in channels:
                    message_counts[ch.name].append(len(ch.sent_messages))

        # Verify message count parity — each channel should have received
        # the same number of messages at each point
        ref_counts = message_counts[channels[0].name]
        for ch in channels:
            assert message_counts[ch.name] == ref_counts, (
                f"Message count parity violation: "
                f"{ch.name}={message_counts[ch.name]} vs "
                f"{channels[0].name}={ref_counts}"
            )

        # Verify final content parity (after stripping prefix)
        normalized = {ch.name: _normalize_messages(ch) for ch in channels}
        ref = normalized[channels[0].name]
        for ch in channels:
            assert normalized[ch.name] == ref, (
                f"Full sequence parity violation:\n"
                f"  {ch.name}: {normalized[ch.name]}\n"
                f"  {channels[0].name}: {ref}"
            )


# ---------------------------------------------------------------------------
# 8. Edge cases — messages that historically caused channel divergence
# ---------------------------------------------------------------------------


class TestEdgeCaseParity:
    """Test edge cases that could cause parity issues between channels."""

    async def test_internal_tags_in_result_stripped_for_all(self):
        """<internal> content in agent results should be stripped for ALL channels."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]

        deps = _make_deps(channels)

        async def mock_broadcast(jid, text, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_message(jid, text)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            result="<internal>reasoning here</internal>The visible answer is 42.",
            type="result",
            new_session_id="s1",
        )

        with patch(
            "pynchy.messaging.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ):
            await handle_streamed_output(deps, CHAT_JID, group, result)

        for ch in channels:
            texts = ch.get_texts()
            for text in texts:
                assert "<internal>" not in text, f"{ch.name} leaked internal tags: {text}"
                assert "reasoning here" not in text, f"{ch.name} leaked internal content: {text}"

    async def test_empty_result_parity(self):
        """Empty results should be handled the same across all channels."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = _make_deps(channels)

        async def mock_broadcast(jid, text, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_message(jid, text)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            result="",
            type="result",
            new_session_id="s1",
        )

        with patch(
            "pynchy.messaging.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ):
            sent = await handle_streamed_output(deps, CHAT_JID, group, result)

        assert sent is False  # Empty result = nothing to send

        # No channel should have received anything
        for ch in channels:
            assert len(ch.sent_messages) == 0, (
                f"{ch.name} received message for empty result: {ch.get_texts()}"
            )

    async def test_result_metadata_parity(self):
        """Cost/usage metadata should be broadcast identically."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = _make_deps(channels)

        async def mock_broadcast(jid, text, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_message(jid, text)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        result = ContainerOutput(
            status="success",
            result="Done.",
            type="result",
            new_session_id="s1",
            result_metadata={
                "total_cost_usd": 0.42,
                "duration_ms": 3200,
                "num_turns": 5,
            },
        )

        with patch(
            "pynchy.messaging.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ):
            await handle_streamed_output(deps, CHAT_JID, group, result)

        # Check that metadata message is consistent
        for ch in channels:
            texts = ch.get_texts()
            meta_msgs = [t for t in texts if "📊" in t]
            assert len(meta_msgs) == 1, f"{ch.name} got {len(meta_msgs)} metadata messages"
            assert "0.42 USD" in meta_msgs[0]
            assert "3.2s" in meta_msgs[0]
            assert "5 turns" in meta_msgs[0]

        # Metadata text should be identical (no prefix differences for metadata)
        all_meta = []
        for ch in channels:
            meta = [t for t in ch.get_texts() if "📊" in t]
            all_meta.append(meta)
        for i, _ch in enumerate(channels):
            assert all_meta[i] == all_meta[0], (
                f"Metadata parity: {ch.name}={all_meta[i]} vs {channels[0].name}={all_meta[0]}"
            )

    async def test_verbose_tool_result_parity(self):
        """ExitPlanMode tool results should show content on all channels."""
        channels = [make_tui_channel(), make_whatsapp_channel(), make_slack_channel()]
        deps = _make_deps(channels)

        async def mock_broadcast(jid, text, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_message(jid, text)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        group = RegisteredGroup(name="Test", folder="test", trigger="@pynchy", added_at="")

        # First send a tool_use for ExitPlanMode to set up _last_tool_name
        tool_use = ContainerOutput(
            status="success",
            type="tool_use",
            tool_name="ExitPlanMode",
            tool_input={},
        )
        with patch(
            "pynchy.messaging.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ):
            await handle_streamed_output(deps, CHAT_JID, group, tool_use)

        for ch in channels:
            ch.clear()

        # Now send the tool_result
        tool_result = ContainerOutput(
            status="success",
            type="tool_result",
            tool_result_id="tr-plan",
            tool_result_content="## Implementation Plan\n1. Step one\n2. Step two",
        )

        with patch(
            "pynchy.messaging.output_handler.store_message_direct",
            new_callable=AsyncMock,
        ):
            await handle_streamed_output(deps, CHAT_JID, group, tool_result)

        # All channels should show the plan content, not just "📋 tool result"
        for ch in channels:
            texts = ch.get_texts()
            assert any("Implementation Plan" in t for t in texts), (
                f"{ch.name} should show ExitPlanMode content, got: {texts}"
            )

        # Content should be identical across channels
        all_texts = [ch.get_texts() for ch in channels]
        for i, _ch in enumerate(channels):
            assert all_texts[i] == all_texts[0], (
                f"Verbose tool result parity: {ch.name}={all_texts[i]}"
            )
