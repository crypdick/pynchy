"""Tests for routing and group availability."""

from __future__ import annotations

import pytest

from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.messaging.formatter import format_messages_for_sdk
from pynchy.state import _init_test_database, store_chat_metadata
from pynchy.types import NewMessage, WorkspaceProfile


@pytest.fixture
async def app():
    """Create a PynchyApp with a fresh in-memory database."""
    await _init_test_database()
    return PynchyApp()


class _PrefixChannel:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.name = "test-prefix-channel"

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith(self.prefix)


# --- get_available_groups ---


class TestGetAvailableGroups:
    async def test_returns_all_chats_when_no_channels_loaded(self, app: PynchyApp):
        await store_chat_metadata("chan://group-1", "2024-01-01T00:00:01.000Z", "Group 1")
        await store_chat_metadata("dm://alice", "2024-01-01T00:00:02.000Z", "Alice DM")
        await store_chat_metadata("chan://group-2", "2024-01-01T00:00:03.000Z", "Group 2")

        groups = await app.get_available_groups()
        assert len(groups) == 3
        assert {g["jid"] for g in groups} == {"chan://group-1", "dm://alice", "chan://group-2"}

    async def test_filters_to_channel_owned_jids_when_channels_loaded(self, app: PynchyApp):
        await store_chat_metadata("chan://group-1", "2024-01-01T00:00:01.000Z", "Group 1")
        await store_chat_metadata("dm://alice", "2024-01-01T00:00:02.000Z", "Alice DM")
        await store_chat_metadata("chan://group-2", "2024-01-01T00:00:03.000Z", "Group 2")

        app.channels = [_PrefixChannel("chan://")]

        groups = await app.get_available_groups()
        assert len(groups) == 2
        assert {g["jid"] for g in groups} == {"chan://group-1", "chan://group-2"}

    async def test_excludes_group_sync_sentinel(self, app: PynchyApp):
        await store_chat_metadata("__group_sync__", "2024-01-01T00:00:00.000Z")
        await store_chat_metadata("chan://group", "2024-01-01T00:00:01.000Z", "Group")

        groups = await app.get_available_groups()
        assert len(groups) == 1
        assert groups[0]["jid"] == "chan://group"

    async def test_marks_registered_groups_correctly(self, app: PynchyApp):
        await store_chat_metadata("chan://reg", "2024-01-01T00:00:01.000Z", "Registered")
        await store_chat_metadata("chan://unreg", "2024-01-01T00:00:02.000Z", "Unregistered")

        app.workspaces = {
            "chan://reg": WorkspaceProfile(
                jid="chan://reg",
                name="Registered",
                folder="registered",
                trigger="@pynchy",
                added_at="2024-01-01T00:00:00.000Z",
            ),
        }

        groups = await app.get_available_groups()
        reg = next(g for g in groups if g["jid"] == "chan://reg")
        unreg = next(g for g in groups if g["jid"] == "chan://unreg")

        assert reg["isRegistered"] is True
        assert unreg["isRegistered"] is False

    async def test_returns_groups_ordered_by_most_recent_activity(self, app: PynchyApp):
        await store_chat_metadata("chan://old", "2024-01-01T00:00:01.000Z", "Old")
        await store_chat_metadata("chan://new", "2024-01-01T00:00:05.000Z", "New")
        await store_chat_metadata("chan://mid", "2024-01-01T00:00:03.000Z", "Mid")

        groups = await app.get_available_groups()
        assert groups[0]["jid"] == "chan://new"
        assert groups[1]["jid"] == "chan://mid"
        assert groups[2]["jid"] == "chan://old"

    async def test_returns_empty_when_no_chats(self, app: PynchyApp):
        groups = await app.get_available_groups()
        assert len(groups) == 0


# --- format_messages_for_sdk ---


def _msg(
    *,
    content: str = "hello",
    message_type: str = "user",
    sender: str = "user@s.whatsapp.net",
    sender_name: str = "Alice",
    timestamp: str = "2024-01-01T00:00:01.000Z",
    metadata: dict | None = None,
) -> NewMessage:
    return NewMessage(
        id="m1",
        chat_jid="group@g.us",
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        message_type=message_type,
        metadata=metadata,
    )


class TestFormatMessagesForSdk:
    """Test format_messages_for_sdk which converts NewMessages to SDK dicts.

    This is the critical boundary between stored messages and what the LLM sees.
    Bugs here can leak host messages to the LLM or drop user messages.
    """

    def test_converts_user_message_to_sdk_format(self):
        msgs = [_msg(content="hello world")]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 1
        assert result[0]["message_type"] == "user"
        assert result[0]["sender"] == "user@s.whatsapp.net"
        assert result[0]["sender_name"] == "Alice"
        assert result[0]["content"] == "hello world"
        assert result[0]["timestamp"] == "2024-01-01T00:00:01.000Z"

    def test_filters_out_host_messages(self):
        """Host messages are operational and must NEVER be sent to the LLM."""
        msgs = [
            _msg(content="user question", message_type="user"),
            _msg(content="⚠️ Agent error occurred", message_type="host"),
            _msg(content="another question", message_type="user"),
        ]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 2
        assert all(m["message_type"] != "host" for m in result)

    def test_preserves_assistant_messages(self):
        msgs = [_msg(content="I'll help with that", message_type="assistant")]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 1
        assert result[0]["message_type"] == "assistant"

    def test_preserves_system_messages(self):
        msgs = [_msg(content="System context update", message_type="system")]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 1
        assert result[0]["message_type"] == "system"

    def test_preserves_tool_result_messages(self):
        msgs = [_msg(content="command output", message_type="tool_result")]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 1
        assert result[0]["message_type"] == "tool_result"

    def test_preserves_metadata(self):
        msgs = [_msg(content="hello", metadata={"source": "whatsapp"})]
        result = format_messages_for_sdk(msgs)
        assert result[0]["metadata"] == {"source": "whatsapp"}

    def test_preserves_none_metadata(self):
        msgs = [_msg(content="hello", metadata=None)]
        result = format_messages_for_sdk(msgs)
        assert result[0]["metadata"] is None

    def test_preserves_message_order(self):
        msgs = [
            _msg(content="first", timestamp="2024-01-01T00:00:01.000Z"),
            _msg(content="second", timestamp="2024-01-01T00:00:02.000Z"),
            _msg(content="third", timestamp="2024-01-01T00:00:03.000Z"),
        ]
        result = format_messages_for_sdk(msgs)
        assert [m["content"] for m in result] == ["first", "second", "third"]

    def test_returns_empty_list_for_no_messages(self):
        assert format_messages_for_sdk([]) == []

    def test_returns_empty_list_when_all_messages_are_host(self):
        msgs = [
            _msg(content="host msg 1", message_type="host"),
            _msg(content="host msg 2", message_type="host"),
        ]
        assert format_messages_for_sdk(msgs) == []

    def test_mixed_message_types_with_host_filtering(self):
        """Realistic scenario: conversation interleaved with host notifications."""
        msgs = [
            _msg(content="@pynchy help me", message_type="user"),
            _msg(content="thinking...", message_type="assistant"),
            _msg(content="🗑️", message_type="host"),
            _msg(content="tool output", message_type="tool_result"),
            _msg(content="⚠️ error", message_type="host"),
            _msg(content="Here's the answer", message_type="assistant"),
        ]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 4
        types = [m["message_type"] for m in result]
        assert types == ["user", "assistant", "tool_result", "assistant"]
