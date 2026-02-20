"""WhatsApp channel using neonize (whatsmeow Python bindings)."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from neonize.aioze import client as neonize_client
from neonize.aioze import events as neonize_events
from neonize.aioze.client import NewAClient
from neonize.events import (
    ConnectedEv,
    ConnectFailureEv,
    DisconnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
)
from neonize.proto.Neonize_pb2 import JID
from neonize.utils.jid import Jid2String

from pynchy.config import get_settings
from pynchy.db import get_last_group_sync, set_last_group_sync, update_chat_name
from pynchy.logger import logger
from pynchy.types import NewMessage, WorkspaceProfile

GROUP_SYNC_INTERVAL: float = 24 * 60 * 60  # 24 hours in seconds


@dataclass
class _OutgoingMessage:
    jid: str
    text: str


class WhatsAppChannel:
    """WhatsApp channel implemented via neonize (whatsmeow Go bindings)."""

    name = "whatsapp"
    prefix_assistant_name = True

    def __init__(
        self,
        on_message: Callable[[str, NewMessage], None],
        on_chat_metadata: Callable[[str, str, str | None], None],
        workspaces: Callable[[], dict[str, WorkspaceProfile]],
    ) -> None:
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._workspaces = workspaces
        self._connected = False
        self._lid_to_phone: dict[str, str] = {}
        self._outgoing_queue: deque[_OutgoingMessage] = deque()
        self._flushing = False
        self._group_sync_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._first_connect: asyncio.Event = asyncio.Event()

        loop = asyncio.get_running_loop()
        neonize_events.event_global_loop = loop
        neonize_client.event_global_loop = loop

        store_dir = get_settings().store_dir
        auth_db = str(store_dir / "neonize.db")
        store_dir.mkdir(parents=True, exist_ok=True)
        self._client = NewAClient(auth_db)
        self._register_events()

    def _register_events(self) -> None:
        @self._client.event(ConnectedEv)
        async def on_connected(_client: NewAClient, _ev: ConnectedEv) -> None:
            self._connected = True
            logger.info("Connected to WhatsApp")
            if self._client.me:
                device = self._client.me
                jid = getattr(device, "JID", None)
                lid = getattr(device, "LID", None)
                if jid and lid and lid.User:
                    self._lid_to_phone[lid.User] = f"{jid.User}@s.whatsapp.net"

            asyncio.ensure_future(self._flush_outgoing_queue())
            asyncio.ensure_future(self._sync_group_metadata())
            if self._group_sync_task is None:
                self._group_sync_task = asyncio.ensure_future(self._periodic_group_sync())
            self._first_connect.set()

        @self._client.event(DisconnectedEv)
        async def on_disconnected(_client: NewAClient, _ev: DisconnectedEv) -> None:
            self._connected = False

        @self._client.event(LoggedOutEv)
        async def on_logged_out(_client: NewAClient, _ev: LoggedOutEv) -> None:
            self._connected = False
            logger.error(
                "Logged out from WhatsApp. Run 'uv run pynchy-whatsapp-auth' to re-authenticate."
            )
            sys.exit(0)

        @self._client.event(ConnectFailureEv)
        async def on_connect_failure(_client: NewAClient, _ev: ConnectFailureEv) -> None:
            self._connected = False
            logger.error("WhatsApp connection failed")

        @self._client.event(PairStatusEv)
        async def on_pair_status(_client: NewAClient, ev: PairStatusEv) -> None:
            logger.info("WhatsApp paired", user=ev.ID.User)

        @self._client.event(MessageEv)
        async def on_message(_client: NewAClient, message: MessageEv) -> None:
            try:
                await self._handle_message(message)
            except Exception:
                logger.exception(
                    "Unhandled error in message handler",
                    message_id=getattr(getattr(message, "Info", None), "ID", "unknown"),
                )

    async def connect(self) -> None:
        @self._client.event.qr
        async def on_qr(_client: NewAClient, qr_data: bytes) -> None:
            logger.error("WhatsApp authentication required. Run: uv run pynchy-whatsapp-auth")
            await asyncio.sleep(1)
            sys.exit(1)

        await self._client.connect()
        self._idle_task = asyncio.ensure_future(self._client.idle())
        await self._first_connect.wait()

    async def send_message(self, jid: str, text: str) -> None:
        if not self._connected:
            self._outgoing_queue.append(_OutgoingMessage(jid=jid, text=text))
            return
        try:
            target = self._parse_jid(jid)
            await self._client.send_message(target, text)
        except Exception as err:
            self._outgoing_queue.append(_OutgoingMessage(jid=jid, text=text))
            logger.warning("Failed to send, message queued", jid=jid, error=str(err))

    async def disconnect(self) -> None:
        self._connected = False
        if self._group_sync_task:
            self._group_sync_task.cancel()
        if self._idle_task:
            self._idle_task.cancel()
        with contextlib.suppress(Exception):
            await self._client.disconnect()

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        try:
            target = self._parse_jid(jid)
            from neonize.utils.enum import ChatPresence, ChatPresenceMedia

            presence = (
                ChatPresence.CHAT_PRESENCE_COMPOSING
                if is_typing
                else ChatPresence.CHAT_PRESENCE_PAUSED
            )
            await self._client.send_chat_presence(
                target, presence, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
            )
        except Exception as err:
            logger.debug("Failed to update typing status", jid=jid, error=str(err))

    async def send_reaction(
        self, chat_jid: str, message_id: str, sender_jid: str, emoji: str
    ) -> None:
        try:
            chat = self._parse_jid(chat_jid)
            sender = self._parse_jid(sender_jid)
            reaction_msg = await self._client.build_reaction(chat, sender, message_id, emoji)
            await self._client.send_message(chat, reaction_msg)
        except Exception as err:
            logger.debug("Failed to send reaction", chat_jid=chat_jid, error=str(err))

    async def mark_message_read(self, chat_jid: str, message_id: str, sender_jid: str) -> None:
        try:
            from neonize.utils.enum import ReceiptType

            chat = self._parse_jid(chat_jid)
            sender = self._parse_jid(sender_jid)
            await self._client.mark_read(
                message_id,
                chat=chat,
                sender=sender,
                receipt=ReceiptType.READ,
            )
        except Exception as err:
            logger.debug("Failed to mark message as read", chat_jid=chat_jid, error=str(err))

    async def create_group(self, name: str) -> str:
        group_info = await self._client.create_group(name)
        return Jid2String(group_info.JID)

    async def sync_group_metadata(self, force: bool = False) -> None:
        await self._sync_group_metadata(force=force)

    async def _sync_group_metadata(self, force: bool = False) -> None:
        if not force:
            last_sync = await get_last_group_sync()
            if last_sync:
                last_sync_time = datetime.fromisoformat(last_sync)
                elapsed = (datetime.now(UTC) - last_sync_time).total_seconds()
                if elapsed < GROUP_SYNC_INTERVAL:
                    return
        try:
            groups = await self._client.get_joined_groups()
            count = 0
            for group in groups:
                name = group.GroupName.Name
                if name:
                    group_jid = Jid2String(group.JID)
                    await update_chat_name(group_jid, name)
                    count += 1
            await set_last_group_sync()
            logger.info("Group metadata synced", count=count)
        except Exception as err:
            logger.error("Failed to sync group metadata", error=str(err))

    async def _periodic_group_sync(self) -> None:
        while True:
            await asyncio.sleep(GROUP_SYNC_INTERVAL)
            try:
                await self._sync_group_metadata()
            except Exception as err:
                logger.error("Periodic group sync failed", error=str(err))

    async def _flush_outgoing_queue(self) -> None:
        if self._flushing or not self._outgoing_queue:
            return
        self._flushing = True
        try:
            while self._outgoing_queue:
                item = self._outgoing_queue.popleft()
                await self.send_message(item.jid, item.text)
        finally:
            self._flushing = False

    async def _handle_message(self, message: MessageEv) -> None:
        info = message.Info
        source = info.MessageSource
        raw_jid = Jid2String(source.Chat)
        if not raw_jid or raw_jid == "status@broadcast":
            return
        chat_jid = self._translate_jid(raw_jid, source.Chat)
        ts = info.Timestamp
        if ts > 1e10:
            ts = ts / 1000
        timestamp = datetime.fromtimestamp(ts, tz=UTC).isoformat()
        self._on_chat_metadata(chat_jid, timestamp, None)

        groups = self._workspaces()
        if chat_jid not in groups:
            return

        msg = message.Message
        content = (
            msg.conversation
            or msg.extendedTextMessage.text
            or msg.imageMessage.caption
            or msg.videoMessage.caption
            or ""
        )
        if source.IsFromMe and content.startswith(f"{get_settings().agent.name}:"):
            return

        sender_jid = Jid2String(source.Sender)
        sender_name = info.Pushname or source.Sender.User or sender_jid.split("@")[0]
        new_msg = NewMessage(
            id=info.ID,
            chat_jid=chat_jid,
            sender=sender_jid,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
            is_from_me=source.IsFromMe,
        )
        self._on_message(chat_jid, new_msg)

    def _translate_jid(self, jid_str: str, jid: JID) -> str:
        if jid.Server != "lid":
            return jid_str
        lid_user = jid.User.split(":")[0]
        phone_jid = self._lid_to_phone.get(lid_user)
        if phone_jid:
            return phone_jid
        return jid_str

    @staticmethod
    def _parse_jid(jid_str: str) -> JID:
        from neonize.utils.jid import build_jid

        if "@" not in jid_str:
            return build_jid(jid_str)
        user, server = jid_str.split("@", 1)
        return build_jid(user, server)

    def is_connected(self) -> bool:
        return self._connected

    async def reconnect(self) -> None:
        logger.info("WhatsApp reconnecting")
        await self.disconnect()
        await self.connect()

    def owns_jid(self, jid: str) -> bool:
        return jid.endswith("@g.us") or jid.endswith("@s.whatsapp.net")

    async def fetch_inbound_since(
        self, channel_jid: str, since: str  # noqa: ARG002
    ) -> list[NewMessage]:
        # WhatsApp has no "fetch history since timestamp" API.  Neonize
        # exposes HistorySyncEv (bootstrap + on-demand via
        # build_history_sync_request), but it requires an anchor message
        # ID to page from — not a timestamp.  Until we register a
        # HistorySyncEv handler to capture the bootstrap sync WhatsApp
        # pushes on connect, dropped messages on this channel are
        # unrecoverable by the reconciler.
        return []
