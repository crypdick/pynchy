"""WhatsApp channel using neonize (whatsmeow Python bindings)."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections import deque
from collections.abc import (
    Awaitable,
    Callable,
    Coroutine,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
from neonize.utils.enum import ChatPresence, ChatPresenceMedia
from neonize.utils.jid import Jid2String, build_jid

from pynchy.host.orchestrator.api import (
    PENDING_QUESTION_TIMEOUT_SECONDS,
    TextFormatter,
    find_pending_for_jid,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    InboundFetchResult,
    NewMessage,
    OutboundEvent,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

from .ask_user import resolve_ask_user_answer

GROUP_SYNC_INTERVAL: float = 24 * 60 * 60  # 24 hours in seconds


@dataclass
class _OutgoingMessage:
    jid: str
    text: str


@dataclass(frozen=True)
class _InboundMessageContext:
    chat_jid: str
    sender_jid: str
    sender_name: str
    timestamp: str
    message_id: str
    is_from_me: bool


class WhatsAppChannel:
    """WhatsApp channel implemented via neonize (whatsmeow Go bindings)."""

    name: str
    prefix_assistant_name = True

    def __init__(  # noqa: PLR0913 - WhatsApp channel constructor is a boundary configuration surface.
        self,
        connection_name: str,
        auth_db_path: str,
        assistant_name: str,
        on_message: Callable[[str, NewMessage], None],
        on_chat_metadata: Callable[[str, str, str | None], None],
        workspaces: Callable[[], dict[str, WorkspaceProfile]],
        on_ask_user_answer: Callable[[str, dict[str, Any]], None] | None = None,
        *,
        client_factory: Callable[[str], Any] | None = None,
        find_chat_jids_by_name: Callable[[str], Awaitable[list[str]]] | None = None,
        get_last_group_sync: Callable[[], Awaitable[str | None]] | None = None,
        set_last_group_sync: Callable[[], Awaitable[None]] | None = None,
        update_chat_name: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.name = connection_name
        self.formatter = TextFormatter()
        self._connection_name = connection_name
        self._auth_db_path = auth_db_path
        self._assistant_name = assistant_name
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._workspaces = workspaces
        self._on_ask_user_answer = on_ask_user_answer
        self._find_chat_jids_by_name = find_chat_jids_by_name
        self._get_last_group_sync = get_last_group_sync
        self._set_last_group_sync = set_last_group_sync
        self._update_chat_name = update_chat_name
        self._connected = False
        self._lid_to_phone: dict[str, str] = {}
        self._outgoing_queue: deque[_OutgoingMessage] = deque()
        self._flushing = False
        self._group_sync_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        # One-shot fire-and-forget tasks (flush, initial group sync) — tracked
        # here so the event loop can't GC them mid-flight, and so disconnect()
        # can cancel any still in progress.
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._first_connect: asyncio.Event = asyncio.Event()

        loop = asyncio.get_running_loop()
        neonize_events.event_global_loop = loop  # noqa: V101
        neonize_client.event_global_loop = loop  # noqa: V101

        auth_db = self._auth_db_path
        Path(auth_db).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._client = (client_factory or NewAClient)(auth_db)
        self._register_events()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Fire-and-forget a coroutine, tracking it so it can't be GC'd or leaked."""
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _register_events(self) -> None:
        @self._client.event(ConnectedEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
        async def on_connected(_client: NewAClient, _ev: ConnectedEv) -> None:  # noqa: RUF029 - neonize may await events.
            self._connected = True
            logger.info("Connected to WhatsApp")
            if self._client.me:
                device = self._client.me
                jid = getattr(device, "JID", None)
                lid = getattr(device, "LID", None)
                if jid and lid and lid.User:
                    self._lid_to_phone[lid.User] = f"{jid.User}@s.whatsapp.net"

            self._spawn(self._flush_outgoing_queue())
            self._spawn(self.sync_group_metadata())
            if self._group_sync_task is None:
                self._group_sync_task = asyncio.ensure_future(self._periodic_group_sync())
            self._first_connect.set()

        @self._client.event(DisconnectedEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
        async def on_disconnected(_client: NewAClient, _ev: DisconnectedEv) -> None:  # noqa: RUF029 - neonize may await events.
            self._connected = False

        @self._client.event(LoggedOutEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
        async def on_logged_out(_client: NewAClient, _ev: LoggedOutEv) -> None:  # noqa: RUF029 - neonize may await events.
            self._connected = False
            logger.error(
                "Logged out from WhatsApp. Run 'uv run pynchy-whatsapp-auth' to re-authenticate."
            )
            sys.exit(0)

        @self._client.event(ConnectFailureEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
        async def on_connect_failure(_client: NewAClient, _ev: ConnectFailureEv) -> None:  # noqa: RUF029 - neonize may await events.
            self._connected = False
            logger.error("WhatsApp connection failed")

        @self._client.event(PairStatusEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
        async def on_pair_status(_client: NewAClient, ev: PairStatusEv) -> None:  # noqa: RUF029 - neonize may await events.
            logger.info("WhatsApp paired", user=ev.ID.User)

        @self._client.event(MessageEv)  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
        async def on_message(_client: NewAClient, message: MessageEv) -> None:
            try:
                await self.ingest_inbound_message(message)
            except Exception:  # noqa: BLE001 - WhatsApp message handler isolation keeps the client alive.
                logger.exception(
                    "Unhandled error in message handler",
                    message_id=getattr(getattr(message, "Info", None), "ID", "unknown"),
                )

    async def connect(self) -> None:
        @self._client.event.qr  # type: ignore[untyped-decorator]  # neonize event decorator is untyped
        async def on_qr(_client: NewAClient, qr_data: bytes) -> None:
            _ = qr_data  # neonize supplies the QR payload for this event.
            logger.error("WhatsApp authentication required. Run: uv run pynchy-whatsapp-auth")
            await asyncio.sleep(1)
            sys.exit(1)

        await self._client.connect()
        self._idle_task = asyncio.ensure_future(self._client.idle())
        await self._first_connect.wait()

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        """Render an outbound event via the formatter and send the text."""
        rendered = self.formatter.render(event)
        await self._send_text(jid, rendered.text)

    async def _send_text(self, jid: str, text: str) -> None:
        """Send raw text to a JID, queueing if disconnected.

        This is the internal transport method -- external callers should use
        ``send_event`` instead.  Kept for queue flush and ``send_ask_user``
        which build their own text payloads.
        """
        if not self._connected:
            self._outgoing_queue.append(_OutgoingMessage(jid=jid, text=text))
            return
        try:
            target = self._parse_jid(jid)
            await self._client.send_message(target, text)
        except Exception as err:  # noqa: BLE001 - send failures are queued for later retry.
            self._outgoing_queue.append(_OutgoingMessage(jid=jid, text=text))
            logger.warning("Failed to send, message queued", jid=jid, error=str(err))

    async def disconnect(self) -> None:
        self._connected = False
        if self._group_sync_task:
            self._group_sync_task.cancel()
        if self._idle_task:
            self._idle_task.cancel()
        for task in self._background_tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await self._client.disconnect()

    async def set_typing(self, jid: str, *, is_typing: bool) -> None:
        try:
            target = self._parse_jid(jid)

            presence = (
                ChatPresence.CHAT_PRESENCE_COMPOSING
                if is_typing
                else ChatPresence.CHAT_PRESENCE_PAUSED
            )
            await self._client.send_chat_presence(
                target, presence, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
            )
        except Exception as err:  # noqa: BLE001 - typing updates are best-effort transport hints.
            logger.debug("Failed to update typing status", jid=jid, error=str(err))

    async def send_reaction(
        self, chat_jid: str, message_id: str, sender_jid: str, emoji: str
    ) -> None:
        try:
            chat = self._parse_jid(chat_jid)
            sender = self._parse_jid(sender_jid)
            reaction_msg = await self._client.build_reaction(chat, sender, message_id, emoji)
            await self._client.send_message(chat, reaction_msg)
        except Exception as err:  # noqa: BLE001 - reaction send failures are best-effort only.
            logger.debug("Failed to send reaction", chat_jid=chat_jid, error=str(err))

    async def create_group(self, name: str) -> str:
        group_info = await self._client.create_group(name)
        return cast("str", Jid2String(group_info.JID))

    async def resolve_chat_jid(self, chat_name: str) -> str | None:
        """Resolve a WhatsApp chat name to a JID using stored metadata."""
        if "@" in chat_name:
            return chat_name
        await self._sync_group_metadata(force=True)
        find_chat_jids_by_name = self._find_chat_jids_by_name
        if find_chat_jids_by_name is None:
            return None
        matches = await find_chat_jids_by_name(chat_name)
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "Multiple WhatsApp chats match name; disambiguate",
                chat=chat_name,
                matches=matches,
            )
            return None
        return matches[0]

    async def sync_group_metadata(self, *, force: bool = False) -> None:
        await self._sync_group_metadata(force=force)

    async def _sync_group_metadata(self, *, force: bool = False) -> None:
        if not force:
            get_last_group_sync = self._get_last_group_sync
            last_sync = await get_last_group_sync() if get_last_group_sync is not None else None
            if last_sync:
                last_sync_time = datetime.fromisoformat(last_sync)
                elapsed = (datetime.now(UTC) - last_sync_time).total_seconds()
                if elapsed < GROUP_SYNC_INTERVAL:
                    return
        try:
            groups = await self._client.get_joined_groups()
            count = await self._apply_group_metadata(groups)
            await self._record_group_sync()
            logger.info("Group metadata synced", count=count)
        except Exception as err:  # noqa: BLE001 - group sync is best-effort background maintenance.
            logger.error("Failed to sync group metadata", error=str(err))

    async def _record_group_sync(self) -> None:
        set_last_group_sync = self._set_last_group_sync
        if set_last_group_sync is not None:
            await set_last_group_sync()

    async def _apply_group_metadata(self, groups: list[Any]) -> int:
        count = 0
        for group in groups:
            name = group.GroupName.Name
            if name:
                group_jid = Jid2String(group.JID)
                update_chat_name = self._update_chat_name
                if update_chat_name is not None:
                    await update_chat_name(group_jid, name)
                count += 1
        return count

    async def _periodic_group_sync(self) -> None:
        while True:
            await asyncio.sleep(GROUP_SYNC_INTERVAL)
            try:
                await self.sync_group_metadata()
            except Exception as err:  # noqa: BLE001 - periodic sync failures should not stop the loop.
                logger.error("Periodic group sync failed", error=str(err))

    async def _flush_outgoing_queue(self) -> None:
        if self._flushing or not self._outgoing_queue:
            return
        self._flushing = True
        try:
            while self._outgoing_queue:
                item = self._outgoing_queue.popleft()
                await self._send_text(item.jid, item.text)
        finally:
            self._flushing = False

    async def send_ask_user(
        self, jid: str, request_id: str, questions: list[dict[str, Any]]
    ) -> str | None:
        """Post a numbered-text question and return a tracking message ID.

        WhatsApp doesn't support interactive widgets, so we format the
        question as numbered text and send it as a regular message.
        """
        lines: list[str] = []
        has_options = False

        for q in questions:
            question_text = q.get("question", "")
            lines.append(f"The agent is asking: {question_text}")
            options = q.get("options", [])
            if options:
                has_options = True
                for i, opt in enumerate(options, 1):
                    label = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                    lines.append(f"{i}. {label}")

        lines.append("")
        if has_options:
            lines.append("Reply with a number or type your own answer.")
        else:
            lines.append("Reply with your answer.")

        text = "\n".join(lines)
        await self._send_text(jid, text)
        # Use request_id as a tracking identifier since WhatsApp _send_text
        # doesn't return a message ID we can use.
        return request_id

    @staticmethod
    def _message_content(message: object) -> str:
        message = cast("Any", message)
        return (
            message.conversation
            or message.extendedTextMessage.text
            or message.imageMessage.caption
            or message.videoMessage.caption
            or ""
        )

    @staticmethod
    def _message_timestamp(raw_timestamp: int | float) -> str:
        if raw_timestamp > 1e10:
            raw_timestamp = raw_timestamp / 1000
        return datetime.fromtimestamp(raw_timestamp, tz=UTC).isoformat()

    def _message_context(self, message: object) -> _InboundMessageContext | None:
        message = cast("Any", message)
        info = message.Info
        source = info.MessageSource
        raw_jid = Jid2String(source.Chat)
        if not raw_jid or raw_jid == "status@broadcast":
            return None
        chat_jid = self._translate_jid(raw_jid, source.Chat)
        if chat_jid not in self._workspaces():
            return None
        sender_jid = Jid2String(source.Sender)
        sender_name = info.Pushname or source.Sender.User or sender_jid.split("@")[0]
        return _InboundMessageContext(
            chat_jid=chat_jid,
            sender_jid=sender_jid,
            sender_name=sender_name,
            timestamp=self._message_timestamp(info.Timestamp),
            message_id=info.ID,
            is_from_me=source.IsFromMe,
        )

    @staticmethod
    def _is_stale_pending_question(pending: dict[str, Any]) -> bool:
        # Skip stale pending questions — let the sweep handle cleanup.
        # A stale file from a crash should not silently swallow real messages.
        timestamp = datetime.fromisoformat(pending.get("timestamp", ""))
        age = (datetime.now(UTC) - timestamp).total_seconds()
        return age > PENDING_QUESTION_TIMEOUT_SECONDS

    def _pending_answer(self, chat_jid: str, content: str) -> tuple[str, dict[str, Any]] | None:
        pending = find_pending_for_jid(chat_jid)
        if pending is None or self._is_stale_pending_question(pending):
            return None
        questions = cast("list[dict[str, Any]]", pending.get("questions", []))
        return pending["request_id"], resolve_ask_user_answer(content, questions)

    def _is_own_agent_echo(self, context: _InboundMessageContext, content: str) -> bool:
        return context.is_from_me and content.startswith(f"{self._assistant_name}:")

    @staticmethod
    def _new_message(context: _InboundMessageContext, content: str) -> NewMessage:
        return NewMessage(
            id=context.message_id,
            chat_jid=context.chat_jid,
            sender=context.sender_jid,
            sender_name=context.sender_name,
            content=content,
            timestamp=context.timestamp,
            is_from_me=context.is_from_me,
        )

    @property
    def on_ask_user_answer(self) -> Callable[[str, dict[str, Any]], None] | None:
        """Callback that receives answers intercepted from pending questions."""
        return self._on_ask_user_answer

    async def ingest_inbound_message(self, message: object) -> None:
        """Ingest one message event received from the WhatsApp SDK."""
        context = self._message_context(message)
        if context is None:
            return
        self._on_chat_metadata(context.chat_jid, context.timestamp, None)

        content = self._message_content(message.Message)
        if self._is_own_agent_echo(context, content):
            return

        if not context.is_from_me:
            pending_answer = self._pending_answer(context.chat_jid, content)
            if pending_answer is not None:
                request_id, answer = pending_answer
                if self._on_ask_user_answer:
                    self._on_ask_user_answer(request_id, answer)
                return

        self._on_message(context.chat_jid, self._new_message(context, content))

    def _translate_jid(self, jid_str: str, jid: object) -> str:
        jid = cast("Any", jid)
        if jid.Server != "lid":
            return jid_str
        lid_user = jid.User.split(":")[0]
        phone_jid = self._lid_to_phone.get(lid_user)
        if phone_jid:
            return phone_jid
        return jid_str

    @staticmethod
    def _parse_jid(jid_str: str) -> object:
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
        return jid.endswith(("@g.us", "@s.whatsapp.net"))

    async def fetch_inbound_since(
        self,
        _channel_jid: str,
        _since: str,
    ) -> InboundFetchResult:
        # WhatsApp has no "fetch history since timestamp" API.  Neonize
        # exposes HistorySyncEv (bootstrap + on-demand via
        # build_history_sync_request), but it requires an anchor message
        # ID to page from — not a timestamp.  Until we register a
        # HistorySyncEv handler to capture the bootstrap sync WhatsApp
        # pushes on connect, dropped messages on this channel are
        # unrecoverable by the reconciler.
        return InboundFetchResult(messages=[])
