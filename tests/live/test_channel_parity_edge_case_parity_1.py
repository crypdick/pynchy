"""Channel parity tests for the built-in messaging channels.

These tests synthesize various message types (agent results, host messages, tool
traces, system events, etc.), push them through the common broadcasting code
paths, and verify that all channels receive equivalent output.

"Equivalent" accounts for known, intentional differences:
- Slack omits the assistant name prefix (the platform shows bot identity)
- WhatsApp prefixes agent messages with the assistant name
- Streaming channels receive updates via post_event/update_event

Run with:
    uv run pytest tests/live/ -m "live and parity"
    uv run pytest tests/live/ -m live
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.host.orchestrator.messaging.router import (
    handle_streamed_output,
)
from pynchy.workspace.api import WorkspaceProfile
from tests.live.channel_parity_support import (
    _make_deps,
)

from .conftest import (
    make_discord_channel,
    make_slack_channel,
    make_whatsapp_channel,
)

pytestmark = [pytest.mark.live, pytest.mark.parity]

CHAT_JID = "group@g.us"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestEdgeCaseParity:
    """Test edge cases that could cause parity issues between channels."""

    @staticmethod
    def _channels():
        return [
            make_whatsapp_channel(),
            make_slack_channel(),
            make_discord_channel(),
        ]

    @classmethod
    def _broadcasting_deps(cls):
        channels = cls._channels()
        deps = _make_deps(channels)

        async def mock_broadcast(jid, event, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_event(jid, event)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None
        return channels, deps

    @staticmethod
    def _group() -> WorkspaceProfile:
        return WorkspaceProfile(
            jid="test@g.us", name="Test", folder="test", trigger="@pynchy", added_at=""
        )

    @staticmethod
    def _metadata_messages(channel) -> list[str]:
        return [text for text in channel.get_texts() if "📊" in text]

    @classmethod
    def _assert_metadata_message(cls, channel) -> None:
        meta_msgs = cls._metadata_messages(channel)
        assert len(meta_msgs) == 1, f"{channel.name} got {len(meta_msgs)} metadata messages"
        assert "0.42 USD" in meta_msgs[0]
        assert "3.2s" in meta_msgs[0]
        assert "5 turns" in meta_msgs[0]

    async def test_internal_tags_in_result_stripped_for_all(self):
        """<internal> content in agent results should be stripped for ALL channels."""
        channels = [
            make_whatsapp_channel(),
            make_slack_channel(),
            make_discord_channel(),
        ]

        deps = _make_deps(channels)

        async def mock_broadcast(jid, event, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_event(jid, event)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        group = WorkspaceProfile(
            jid="test@g.us", name="Test", folder="test", trigger="@pynchy", added_at=""
        )

        result = ContainerOutput(
            status="success",
            result="<internal>reasoning here</internal>The visible answer is 42.",
            type="result",
            new_session_id="s1",
        )

        await handle_streamed_output(deps, CHAT_JID, group, result)

        for ch in channels:
            texts = ch.get_texts()
            for text in texts:
                assert "<internal>" not in text, f"{ch.name} leaked internal tags: {text}"
                assert "reasoning here" not in text, f"{ch.name} leaked internal content: {text}"

    async def test_empty_result_parity(self):
        """Empty results should be handled the same across all channels."""
        channels = [
            make_whatsapp_channel(),
            make_slack_channel(),
            make_discord_channel(),
        ]
        deps = _make_deps(channels)

        async def mock_broadcast(jid, event, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_event(jid, event)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        group = WorkspaceProfile(
            jid="test@g.us", name="Test", folder="test", trigger="@pynchy", added_at=""
        )

        result = ContainerOutput(
            status="success",
            result="",
            type="result",
            new_session_id="s1",
        )

        sent = await handle_streamed_output(deps, CHAT_JID, group, result)

        assert sent is False  # Empty result = nothing to send

        # No channel should have received anything
        for ch in channels:
            assert len(ch.sent_messages) == 0, (
                f"{ch.name} received message for empty result: {ch.get_texts()}"
            )

    async def test_result_metadata_parity(self):
        """Cost/usage metadata should be broadcast identically."""
        channels, deps = self._broadcasting_deps()

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

        await handle_streamed_output(deps, CHAT_JID, self._group(), result)

        # Check that metadata message is consistent
        for ch in channels:
            self._assert_metadata_message(ch)

        # Metadata text should be identical (no prefix differences for metadata)
        all_meta = [self._metadata_messages(ch) for ch in channels]
        for i, _ch in enumerate(channels):
            assert all_meta[i] == all_meta[0], (
                f"Metadata parity: {ch.name}={all_meta[i]} vs {channels[0].name}={all_meta[0]}"
            )

    async def test_verbose_tool_result_parity(self):
        """ExitPlanMode tool results should show content on all channels."""
        channels = [
            make_whatsapp_channel(),
            make_slack_channel(),
            make_discord_channel(),
        ]
        deps = _make_deps(channels)

        async def mock_broadcast(jid, event, **kwargs):
            for ch in channels:
                if ch.is_connected():
                    await ch.send_event(jid, event)

        deps.broadcast_to_channels = AsyncMock(side_effect=mock_broadcast)
        deps.emit = lambda *a, **kw: None

        group = WorkspaceProfile(
            jid="test@g.us", name="Test", folder="test", trigger="@pynchy", added_at=""
        )

        # First send a tool_use for ExitPlanMode to set up _last_tool_name
        tool_use = ContainerOutput(
            status="success",
            type="tool_use",
            tool_name="ExitPlanMode",
            tool_input={},
        )
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
