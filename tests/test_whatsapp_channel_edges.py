"""Public WhatsApp channel behavior beyond ask-user delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.plugins.api import OutboundEvent, OutboundEventType
from pynchy.plugins.channels.whatsapp import WhatsAppChannel
from pynchy.plugins.channels.whatsapp import channel as whatsapp_channel
from tests.test_whatsapp_ask_user import (
    CHAT_JID,
    WORKSPACE,
    _FakeWhatsAppClient,
    _Group,
    _GroupName,
    _inbound_event,
    _Jid,
    _NoopMetadataSyncWhatsAppChannel,
    _pending_data,
)


@dataclass(frozen=True)
class _Device:
    JID: _Jid
    LID: _Jid


class _QrClient(_FakeWhatsAppClient):
    async def connect(self) -> None:
        assert self.event.qr_handler is not None
        await self.event.qr_handler(self, b"qr")


@dataclass(frozen=True)
class _ConnectedChannel:
    channel: WhatsAppChannel
    client: _FakeWhatsAppClient
    on_message: MagicMock
    on_metadata: MagicMock


@pytest.fixture
async def connected_channel(tmp_path) -> _ConnectedChannel:
    client = _FakeWhatsAppClient()
    on_message = MagicMock()
    on_metadata = MagicMock()
    channel = _NoopMetadataSyncWhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=on_message,
        on_chat_metadata=on_metadata,
        workspaces=lambda: {CHAT_JID: WORKSPACE},
        client_factory=lambda _auth_db: client,
    )
    await channel.connect()
    try:
        yield _ConnectedChannel(channel, client, on_message, on_metadata)
    finally:
        await channel.disconnect()


def _channel(
    tmp_path,
    client: _FakeWhatsAppClient,
    *,
    workspaces=None,
    on_answer=None,
    **kwargs,
) -> WhatsAppChannel:
    return _NoopMetadataSyncWhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        workspaces=workspaces or (lambda: {CHAT_JID: WORKSPACE}),
        on_ask_user_answer=on_answer,
        client_factory=lambda _auth_db: client,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_disconnected_send_is_flushed_after_connect(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    channel = _channel(tmp_path, client)

    await channel.send_event(
        CHAT_JID,
        OutboundEvent(type=OutboundEventType.TEXT, content="queued before login"),
    )
    assert client.sent_messages == []

    await channel.connect()
    await asyncio.sleep(0)
    try:
        assert client.sent_messages == [(("120363001234567890", "g.us"), "queued before login")]
    finally:
        await channel.disconnect()


@pytest.mark.asyncio
async def test_failed_send_is_retried_on_next_connection(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    channel = _channel(tmp_path, client)
    await channel.connect()
    failing_send = AsyncMock(side_effect=OSError("transport unavailable"))
    client.send_message = failing_send

    await channel.send_event(
        CHAT_JID,
        OutboundEvent(type=OutboundEventType.TEXT, content="retry me"),
    )

    client.send_message = _FakeWhatsAppClient.send_message.__get__(client)
    await channel.disconnect()
    await channel.connect()
    await asyncio.sleep(0)
    try:
        assert client.sent_messages == [(("120363001234567890", "g.us"), "retry me")]
    finally:
        await channel.disconnect()


@pytest.mark.asyncio
async def test_lid_inbound_message_is_delivered_to_phone_workspace(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    phone_jid = "5551234@s.whatsapp.net"
    on_message = MagicMock()
    on_metadata = MagicMock()
    channel = _NoopMetadataSyncWhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=on_message,
        on_chat_metadata=on_metadata,
        workspaces=lambda: {phone_jid: WORKSPACE},
        client_factory=lambda _auth_db: client,
    )
    client.me = _Device(
        JID=_Jid("5551234@s.whatsapp.net", "5551234", "s.whatsapp.net"),
        LID=_Jid("5551234:device@lid", "5551234", "lid"),
    )

    await channel.connect()
    event = _inbound_event("hello")
    lid_chat = _Jid("5551234:device@lid", "5551234:device", "lid")
    event = replace(
        event,
        Info=replace(event.Info, MessageSource=replace(event.Info.MessageSource, Chat=lid_chat)),
    )
    try:
        await channel.ingest_inbound_message(event)
        assert on_metadata.call_args.args[0] == phone_jid
        assert on_message.call_args.args[0] == phone_jid
    finally:
        await channel.disconnect()


@pytest.mark.asyncio
async def test_connect_without_complete_device_identity_still_connects(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    client.me = MagicMock(JID=None, LID=None)
    channel = _channel(tmp_path, client)

    await channel.connect()

    try:
        assert channel.is_connected() is True
    finally:
        await channel.disconnect()


@pytest.mark.asyncio
async def test_status_and_unknown_chat_messages_are_ignored(connected_channel) -> None:
    status_event = _inbound_event("status")
    status_chat = _Jid("status@broadcast", "status", "broadcast")
    status_event = replace(
        status_event,
        Info=replace(
            status_event.Info,
            MessageSource=replace(status_event.Info.MessageSource, Chat=status_chat),
        ),
    )
    unknown_event = _inbound_event("unknown")
    unknown_chat = _Jid("unknown@g.us", "unknown", "g.us")
    unknown_event = replace(
        unknown_event,
        Info=replace(
            unknown_event.Info,
            MessageSource=replace(unknown_event.Info.MessageSource, Chat=unknown_chat),
        ),
    )

    await connected_channel.channel.ingest_inbound_message(status_event)
    await connected_channel.channel.ingest_inbound_message(unknown_event)

    connected_channel.on_metadata.assert_not_called()
    connected_channel.on_message.assert_not_called()


@pytest.mark.asyncio
async def test_millisecond_provider_timestamp_is_normalized(connected_channel) -> None:
    event = _inbound_event("timestamped")
    event = replace(event, Info=replace(event.Info, Timestamp=17_400_000_000_000))

    await connected_channel.channel.ingest_inbound_message(event)

    timestamp = connected_channel.on_message.call_args.args[1].timestamp
    assert timestamp == datetime.fromtimestamp(17_400_000_000, tz=UTC).isoformat()


@pytest.mark.asyncio
async def test_recent_group_sync_skips_provider_fetch(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    client.get_joined_groups = AsyncMock()
    channel = WhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        workspaces=lambda: {CHAT_JID: WORKSPACE},
        client_factory=lambda _auth_db: client,
        get_last_group_sync=AsyncMock(return_value=datetime.now(UTC).isoformat()),
    )

    await channel.sync_group_metadata()

    client.get_joined_groups.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_sync_fetches_provider_groups_without_prior_sync(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    client.get_joined_groups = AsyncMock(return_value=[])
    channel = WhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        workspaces=lambda: {CHAT_JID: WORKSPACE},
        client_factory=lambda _auth_db: client,
        get_last_group_sync=AsyncMock(return_value=None),
    )

    await channel.sync_group_metadata()

    client.get_joined_groups.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_sync_ignores_unnamed_groups(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    client.get_joined_groups = AsyncMock(
        return_value=[
            _Group(JID=_Jid("empty@g.us", "empty", "g.us"), GroupName=_GroupName("")),
            _Group(JID=_Jid("named@g.us", "named", "g.us"), GroupName=_GroupName("Named")),
        ]
    )
    update_chat_name = AsyncMock()
    channel = WhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        workspaces=lambda: {CHAT_JID: WORKSPACE},
        client_factory=lambda _auth_db: client,
        update_chat_name=update_chat_name,
    )

    await channel.sync_group_metadata(force=True)

    update_chat_name.assert_awaited_once_with("named@g.us", "Named")


@pytest.mark.asyncio
async def test_stale_group_sync_fetches_provider_and_swallows_failure(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    client.get_joined_groups = AsyncMock(side_effect=OSError("provider offline"))
    channel = WhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        workspaces=lambda: {CHAT_JID: WORKSPACE},
        client_factory=lambda _auth_db: client,
        get_last_group_sync=AsyncMock(
            return_value=(datetime.now(UTC) - timedelta(days=2)).isoformat()
        ),
    )

    await channel.sync_group_metadata()

    client.get_joined_groups.assert_awaited_once()


@pytest.mark.asyncio
async def test_named_group_sync_without_update_callback_still_completes(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    client.get_joined_groups = AsyncMock(
        return_value=[
            _Group(JID=_Jid("named@g.us", "named", "g.us"), GroupName=_GroupName("Named"))
        ]
    )
    channel = WhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=MagicMock(),
        on_chat_metadata=MagicMock(),
        workspaces=lambda: {CHAT_JID: WORKSPACE},
        client_factory=lambda _auth_db: client,
    )

    await channel.sync_group_metadata(force=True)

    client.get_joined_groups.assert_awaited_once()


@pytest.mark.asyncio
async def test_periodic_group_sync_survives_provider_failure(tmp_path, monkeypatch) -> None:
    client = _FakeWhatsAppClient()
    channel = _channel(tmp_path, client)
    calls = 0
    periodic_failure_seen = asyncio.Event()

    async def failing_sync() -> None:
        nonlocal calls
        await asyncio.sleep(0)
        calls += 1
        if calls >= 2:
            periodic_failure_seen.set()
        raise OSError("provider offline")

    channel.sync_group_metadata = failing_sync  # type: ignore[method-assign]
    monkeypatch.setattr(whatsapp_channel, "GROUP_SYNC_INTERVAL", 0)

    await channel.connect()
    try:
        await asyncio.wait_for(periodic_failure_seen.wait(), timeout=2)
    finally:
        await channel.disconnect()


@pytest.mark.asyncio
async def test_chat_name_resolution_without_lookup_callback_returns_none(tmp_path) -> None:
    channel = _channel(tmp_path, _FakeWhatsAppClient())

    assert await channel.resolve_chat_jid("missing chat") is None


@pytest.mark.asyncio
async def test_transport_hint_failures_are_best_effort(connected_channel) -> None:
    connected_channel.client.send_chat_presence = AsyncMock(side_effect=OSError("offline"))
    connected_channel.client.build_reaction = AsyncMock(side_effect=OSError("offline"))

    await connected_channel.channel.set_typing(CHAT_JID, is_typing=True)
    await connected_channel.channel.send_reaction(
        CHAT_JID, "message-1", "5551234@s.whatsapp.net", "👍"
    )


@pytest.mark.asyncio
async def test_non_echo_message_from_self_still_reaches_message_callback(connected_channel) -> None:
    await connected_channel.channel.ingest_inbound_message(
        _inbound_event("human-authored", is_from_me=True)
    )

    assert connected_channel.on_message.call_args.args[1].content == "human-authored"


@pytest.mark.asyncio
async def test_pending_answer_without_callback_is_consumed(tmp_path, monkeypatch) -> None:
    client = _FakeWhatsAppClient()
    on_message = MagicMock()
    channel = _NoopMetadataSyncWhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=on_message,
        on_chat_metadata=MagicMock(),
        workspaces=lambda: {CHAT_JID: WORKSPACE},
        client_factory=lambda _auth_db: client,
    )
    await channel.connect()
    monkeypatch.setattr(whatsapp_channel, "find_pending_for_jid", lambda _jid: _pending_data())
    try:
        await channel.ingest_inbound_message(_inbound_event("2"))
    finally:
        await channel.disconnect()

    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_unmapped_lid_is_delivered_using_original_jid(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    raw_lid = "5551234:device@lid"
    on_message = MagicMock()
    channel = _NoopMetadataSyncWhatsAppChannel(
        connection_name="connection.whatsapp.edges",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=on_message,
        on_chat_metadata=MagicMock(),
        workspaces=lambda: {raw_lid: WORKSPACE},
        client_factory=lambda _auth_db: client,
    )
    await channel.connect()
    event = _inbound_event("unmapped")
    event = replace(
        event,
        Info=replace(
            event.Info,
            MessageSource=replace(
                event.Info.MessageSource,
                Chat=_Jid(raw_lid, "5551234:device", "lid"),
            ),
        ),
    )
    try:
        await channel.ingest_inbound_message(event)
    finally:
        await channel.disconnect()

    assert on_message.call_args.args[0] == raw_lid


@pytest.mark.asyncio
async def test_connection_state_reconnect_and_jid_ownership_are_publicly_consistent(
    tmp_path,
) -> None:
    channel = _channel(tmp_path, _FakeWhatsAppClient())

    assert channel.is_connected() is False
    assert channel.owns_jid(CHAT_JID) is True
    assert channel.owns_jid("not-whatsapp") is False
    await channel.connect()
    try:
        assert channel.is_connected() is True
        await channel.reconnect()
        assert channel.is_connected() is True
    finally:
        await channel.disconnect()


@pytest.mark.asyncio
async def test_disconnect_before_connect_is_idempotent(tmp_path) -> None:
    channel = _channel(tmp_path, _FakeWhatsAppClient())

    await channel.disconnect()

    assert channel.is_connected() is False


@pytest.mark.asyncio
async def test_bare_jid_presence_uses_provider_parser(connected_channel) -> None:
    await connected_channel.channel.set_typing("5551234", is_typing=True)

    assert connected_channel.client.presence_updates[-1] == (("5551234",), "composing", "text")


@pytest.mark.asyncio
async def test_connect_failure_marks_channel_disconnected(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    channel = _channel(tmp_path, client)
    await channel.connect()

    await client.event.handlers[whatsapp_channel.ConnectFailureEv](client, object())

    try:
        assert channel.is_connected() is False
    finally:
        await channel.disconnect()


@pytest.mark.asyncio
async def test_logged_out_event_exits_after_clearing_connection(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    channel = _channel(tmp_path, client)
    await channel.connect()

    with pytest.raises(SystemExit, match="0"):
        await client.event.handlers[whatsapp_channel.LoggedOutEv](client, object())

    assert channel.is_connected() is False
    await channel.disconnect()


@pytest.mark.asyncio
async def test_qr_event_exits_when_authentication_is_required(tmp_path) -> None:
    channel = _channel(tmp_path, _QrClient())

    with pytest.raises(SystemExit, match="1"):
        await channel.connect()


@pytest.mark.asyncio
async def test_message_event_handler_contains_ingest_failures(tmp_path) -> None:
    client = _FakeWhatsAppClient()
    channel = _channel(tmp_path, client)
    await channel.connect()
    channel.ingest_inbound_message = AsyncMock(side_effect=RuntimeError("bad message"))

    try:
        await client.event.handlers[whatsapp_channel.MessageEv](client, object())
    finally:
        await channel.disconnect()
