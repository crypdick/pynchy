"""SlackChannel — pynchy Channel protocol implementation backed by Slack Socket Mode.

Implementation is composed from focused collaborator objects in this package,
each constructed with a back-reference to the channel:

- ``_lifecycle``: connect/disconnect/reconnect and reconnect-on-exit
- ``_allowlist``: configured chat allowlist, resolution, and creation
- ``_events``: inbound Slack event routing (messages, mentions, reactions)
- ``_interactions``: Block Kit interactive-callback handlers (ask_user, approvals, stop)

This module is the composition root: it owns all shared connection state,
implements the outbound-facing protocol methods, history catch-up, and
name-resolution helpers, and delegates the lifecycle/allowlist/event surface
to the collaborators (see ``self.lifecycle``/``allowlist``/``events``/``interactions``).
"""

from __future__ import annotations

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
)
from datetime import datetime
from typing import ClassVar, Protocol, cast, runtime_checkable

from pynchy.logger import logger
from pynchy.plugins.api import (  # beartype resolves these runtime annotations.
    InboundFetchResult,
    NewMessage,
    OutboundEvent,
)
from pynchy.plugins.channels.slack._blocks import SlackBlocksFormatter

from ._allowlist import SlackAllowlist
from ._cache import TtlCache
from ._events import SlackEvents
from ._history import SlackHistory
from ._ids import JID_PREFIX, channel_id_from_jid
from ._interactions import SlackInteractions
from ._lifecycle import SlackLifecycle
from ._ui import build_ask_user_blocks, normalize_chat_name, split_text

_SLACK_APP_NOT_INITIALIZED = "Slack app is not initialized"

JsonDict = dict[str, object]


@runtime_checkable
class _SlackClient(Protocol):
    def chat_postMessage(self, **kwargs: object) -> Awaitable[JsonDict]: ...  # noqa: N802 - Slack SDK method name.

    def chat_update(self, **kwargs: object) -> Awaitable[JsonDict]: ...

    def reactions_add(self, **kwargs: object) -> Awaitable[JsonDict]: ...

    def conversations_history(self, **kwargs: object) -> Awaitable[JsonDict]: ...

    def users_info(self, **kwargs: object) -> Awaitable[JsonDict]: ...

    def conversations_info(self, **kwargs: object) -> Awaitable[JsonDict]: ...


@runtime_checkable
class _SlackApp(Protocol):
    client: _SlackClient


class SlackChannel:
    """Pynchy ``Channel`` protocol implementation backed by Slack Socket Mode."""

    prefix_assistant_name: bool = False  # Slack shows the bot username already
    supports_direct_ask_user_callbacks: bool = True  # noqa: V107

    def __init__(  # noqa: PLR0913 - Slack channel constructor is the plugin integration boundary.
        self,
        connection_name: str,
        bot_token: str,
        app_token: str,
        chat_names: list[str],
        assistant_name: str,
        *,
        allow_create: bool,
        on_message: Callable[[str, NewMessage], None],
        on_chat_metadata: Callable[[str, str, str | None], None],
        on_reaction: Callable[[str, str, str, str], None] | None = None,
        on_ask_user_answer: Callable[[str, JsonDict], None] | None = None,
        on_approval_decision: Callable[[str, str, str, str], None] | None = None,
        on_agent_stop: Callable[[str, str], None] | None = None,
    ) -> None:
        self.name = connection_name
        self.formatter = SlackBlocksFormatter()
        self._connection_name = connection_name
        self._bot_token = bot_token
        self._app_token = app_token
        self._chat_names = {normalize_chat_name(name) for name in chat_names}
        self.assistant_name = assistant_name
        self._allow_create = allow_create
        self._chat_name_to_id: dict[str, str] = {}
        self._allowed_channel_ids: set[str] = set()
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._on_reaction = on_reaction
        self._on_ask_user_answer = on_ask_user_answer
        self._on_approval_decision = on_approval_decision
        self._on_agent_stop = on_agent_stop
        self._connected = False
        self._shutting_down = False

        # Assigned when connect() builds the Slack app and socket handler.
        self._app: _SlackApp | None = None
        self._handler: object | None = None
        self._handler_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._bot_user_id: str = ""
        # Dedup: track recent Slack ts values to avoid processing both
        # message + app_mention events for the same user message.
        self._seen_ts: dict[str, float] = {}
        self._seen_ts_max = 500
        # Cache resolved Slack user/channel names to avoid redundant API calls.
        # TTL of 1 hour — names change rarely; bounded to 500 entries.
        self._user_name_cache = TtlCache(ttl_seconds=3600.0, max_size=500)
        self._channel_name_cache = TtlCache(ttl_seconds=3600.0, max_size=500)

        # Composed collaborators. Each holds a back-reference to this channel
        # so it can read/write the shared connection state above (notably the
        # late-bound ``_app`` client, reassigned on every reconnect).
        self.lifecycle = SlackLifecycle(self)
        self.allowlist = SlackAllowlist(self)
        self.events = SlackEvents(self)
        self.history = SlackHistory(self)
        self.interactions = SlackInteractions(self)

    # ------------------------------------------------------------------
    # Lifecycle — delegated to self.lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await self.lifecycle.connect()

    def is_connected(self) -> bool:
        return self.lifecycle.is_connected()

    async def disconnect(self) -> None:
        await self.lifecycle.disconnect()

    async def reconnect(self) -> None:
        await self.lifecycle.reconnect()

    def prepare_shutdown(self) -> None:
        self.lifecycle.prepare_shutdown()

    def handle_socket_mode_exit(self, task: asyncio.Task[None]) -> None:
        """Handle a Socket Mode transport task ending unexpectedly.

        This is the callback boundary between Slack's SDK task and the
        channel lifecycle. It lets an SDK integration report an exit without
        relying on lifecycle collaborator internals.
        """
        self.lifecycle.on_handler_done(task)

    # ------------------------------------------------------------------
    # Allowlist — delegated to self.allowlist
    # ------------------------------------------------------------------

    @property
    def connection_name(self) -> str:
        return self._connection_name

    @property
    def bot_token(self) -> str:
        return self._bot_token

    @property
    def app_token(self) -> str:
        return self._app_token

    @property
    def slack_app(self) -> _SlackApp | None:
        return self._app

    @slack_app.setter
    def slack_app(self, app: _SlackApp | None) -> None:
        self._app = app

    def require_slack_app(self) -> _SlackApp:
        if self._app is None:
            raise RuntimeError(_SLACK_APP_NOT_INITIALIZED)
        return self._app

    @property
    def handler(self) -> object | None:
        return self._handler

    @handler.setter
    def handler(self, handler: object | None) -> None:
        self._handler = handler

    @property
    def handler_task(self) -> asyncio.Task[None] | None:
        return self._handler_task

    @handler_task.setter
    def handler_task(self, task: asyncio.Task[None] | None) -> None:
        self._handler_task = task

    @property
    def reconnect_task(self) -> asyncio.Task[None] | None:
        return self._reconnect_task

    @reconnect_task.setter
    def reconnect_task(self, task: asyncio.Task[None] | None) -> None:
        self._reconnect_task = task

    @property
    def bot_user_id(self) -> str:
        return self._bot_user_id

    @bot_user_id.setter
    def bot_user_id(self, user_id: str) -> None:
        self._bot_user_id = user_id

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, connected: bool) -> None:
        self._connected = connected

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    @shutting_down.setter
    def shutting_down(self, shutting_down: bool) -> None:
        self._shutting_down = shutting_down

    @property
    def on_ask_user_answer(self) -> Callable[[str, JsonDict], None] | None:
        return self._on_ask_user_answer

    @property
    def on_approval_decision(self) -> Callable[[str, str, str, str], None] | None:
        return self._on_approval_decision

    @property
    def on_agent_stop(self) -> Callable[[str, str], None] | None:
        return self._on_agent_stop

    async def resolve_user_name(self, user_id: str) -> str:
        return await self._resolve_user_name(user_id)

    async def resolve_channel_name(self, channel_id: str) -> str:
        return await self._resolve_channel_name(channel_id)

    def emit_chat_metadata(self, jid: str, timestamp: str, name: str | None) -> None:
        self._on_chat_metadata(jid, timestamp, name)

    def emit_message(self, jid: str, message: NewMessage) -> None:
        self._on_message(jid, message)

    def emit_reaction(self, jid: str, timestamp: str, user: str, emoji: str) -> None:
        if self._on_reaction is not None:
            self._on_reaction(jid, timestamp, user, emoji)

    def track_slack_ts(self, ts: str, now: float, *, ttl_seconds: float = 120.0) -> bool:
        if ts in self._seen_ts:
            return True
        if len(self._seen_ts) >= self._seen_ts_max:
            cutoff = now - ttl_seconds
            self._seen_ts = {k: v for k, v in self._seen_ts.items() if v > cutoff}
        self._seen_ts[ts] = now
        return False

    async def sync_allowed_channels(self) -> None:
        await self.allowlist.sync_allowed_channels()

    @property
    def allow_create(self) -> bool:
        return self._allow_create

    @property
    def configured_chat_names(self) -> set[str]:
        return set(self._chat_names)

    def add_configured_chat_name(self, name: str) -> None:
        self._chat_names.add(name)

    def register_allowed_channel(self, name: str, channel_id: str) -> None:
        self._chat_name_to_id[normalize_chat_name(name)] = channel_id
        self._allowed_channel_ids.add(channel_id)

    def allowed_channel_id_for_name(self, name: str) -> str | None:
        return self._chat_name_to_id.get(normalize_chat_name(name))

    def clear_allowed_channels(self) -> None:
        self._allowed_channel_ids = set()
        self._chat_name_to_id = {}

    def is_allowed_channel(self, channel_id: str) -> bool:
        return bool(self._allowed_channel_ids) and channel_id in self._allowed_channel_ids

    def allowed_channel_count(self) -> int:
        return len(self._allowed_channel_ids)

    async def create_group(self, name: str) -> str:
        return await self.allowlist.create_group(name)

    async def resolve_chat_jid(self, chat_name: str) -> str | None:
        return await self.allowlist.resolve_chat_jid(chat_name)

    # ------------------------------------------------------------------
    # Inbound events — delegated to self.events
    # ------------------------------------------------------------------

    def register_inbound_handlers(self) -> None:
        """Register this channel's Slack Bolt event and interaction callbacks.

        Connection setup calls this after the Slack SDK app is available. It
        is also the adapter boundary for hosts that provision the SDK app
        outside ``connect()``.
        """
        self.events.register_handlers()

    async def ingest_inbound_event(self, event: JsonDict) -> None:
        """Ingest one Slack SDK message or mention event.

        Slack Bolt handlers and reconnect-safe adapter tests both enter here.
        The method deliberately exposes the channel boundary, while parsing,
        deduplication, and callback wiring stay behind the event collaborator.
        """
        await self.events.on_slack_message(event)

    # ------------------------------------------------------------------
    # Channel protocol
    # ------------------------------------------------------------------

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        """Render an outbound event and send it to the Slack channel."""
        if not self._app or not self.owns_jid(jid):
            return
        rendered = self.formatter.render(event)
        channel_id = channel_id_from_jid(jid)
        if rendered.blocks:
            await self._app.client.chat_postMessage(
                channel=channel_id, text=rendered.text, blocks=rendered.blocks
            )
        else:
            chunks = split_text(rendered.text, max_len=3000)
            for chunk in chunks:
                await self._app.client.chat_postMessage(channel=channel_id, text=chunk)

    async def post_event(self, jid: str, event: OutboundEvent) -> str | None:
        """Post a rendered event and return its ``ts`` (message ID) for later updates."""
        if not self._app or not self.owns_jid(jid):
            return None
        rendered = self.formatter.render(event)
        channel_id = channel_id_from_jid(jid)
        kwargs: dict[str, object] = {"channel": channel_id, "text": rendered.text}
        if rendered.blocks:
            kwargs["blocks"] = rendered.blocks
        resp = await self._app.client.chat_postMessage(**kwargs)
        return cast("str | None", resp.get("ts"))

    async def update_event(self, jid: str, message_id: str, event: OutboundEvent) -> None:
        """Update an existing Slack message in-place with a rendered event."""
        if not self._app or not self.owns_jid(jid):
            return
        rendered = self.formatter.render(event)
        channel_id = channel_id_from_jid(jid)
        kwargs: dict[str, object] = {"channel": channel_id, "ts": message_id, "text": rendered.text}
        if rendered.blocks:
            kwargs["blocks"] = rendered.blocks
        await self._app.client.chat_update(**kwargs)

    def owns_jid(self, jid: str) -> bool:
        if not jid.startswith(JID_PREFIX):
            return False
        return self.is_allowed_channel(channel_id_from_jid(jid))

    async def set_typing(self, jid: str, *, is_typing: bool) -> None:
        """Slack doesn't have a user-level typing indicator API, so this is a no-op."""

    # Unicode -> Slack emoji name mapping.  Callers may pass either format;
    # Slack's reactions.add API requires the short-code name.
    _UNICODE_TO_SLACK_NAME: ClassVar[dict[str, str]] = {
        "👀": "eyes",
        "🦞": "lobster",
        "🦀": "crab",
        "❌": "x",
    }

    async def send_reaction(
        self,
        jid: str,
        message_id: str,
        _sender: str,
        emoji: str,
    ) -> None:
        """Add a reaction to a Slack message.

        ``message_id`` is a pynchy message ID (e.g. ``slack-{ts}``).  The raw
        Slack ``ts`` is extracted from the prefix.  Non-Slack message IDs are
        silently ignored (no valid Slack ts to react to).
        Accepts either Slack names (``eyes``) or Unicode emoji (``👀``).
        """
        if not self._app or not self.owns_jid(jid):
            return
        # Extract raw Slack ts from the pynchy message ID.
        # Regular messages: "slack-{ts}", assistant: "slack-assistant-{ts}".
        if message_id.startswith("slack-assistant-"):
            slack_ts = message_id.removeprefix("slack-assistant-")
        elif message_id.startswith("slack-"):
            slack_ts = message_id.removeprefix("slack-")
        else:
            logger.debug(
                "send_reaction skipped — not a Slack-originated message",
                message_id=message_id,
            )
            return
        channel_id = channel_id_from_jid(jid)
        # Convert Unicode emoji to Slack name, or strip colons from name format
        emoji_name = self._UNICODE_TO_SLACK_NAME.get(emoji, emoji.strip(":"))
        try:
            await self._app.client.reactions_add(
                channel=channel_id, timestamp=slack_ts, name=emoji_name
            )
        except Exception as exc:  # noqa: BLE001 - Slack reactions are best-effort delivery.
            logger.debug("Slack reaction failed", err=str(exc))

    async def send_ask_user(
        self, jid: str, request_id: str, questions: list[JsonDict]
    ) -> str | None:
        """Post an interactive question widget and return the message ``ts``.

        Builds a Block Kit payload with:
        - A ``section`` block per question (mrkdwn text)
        - An ``actions`` block with buttons if options are provided
        - An ``input`` block with ``plain_text_input`` for free-form answers
        - A submit button for the text input

        The ``request_id`` is embedded in ``block_id`` and ``action_id`` values
        so that interaction callbacks can route answers to the right pending
        question.
        """
        if not self._app or not self.owns_jid(jid):
            return None
        channel_id = channel_id_from_jid(jid)

        blocks = build_ask_user_blocks(request_id, questions)
        # Plain text for notifications / clients that don't render blocks
        fallback = "Question: " + "; ".join(str(q.get("question", "")) for q in questions)

        resp = await self._app.client.chat_postMessage(
            channel=channel_id, blocks=blocks, text=fallback
        )
        return cast("str | None", resp.get("ts"))

    # ------------------------------------------------------------------
    # History catch-up (reconnect recovery)
    # ------------------------------------------------------------------

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        """Fetch Slack messages newer than ``since`` for a single channel.

        The reconciler resolves JIDs before calling — ``channel_jid`` is a
        Slack-native JID like ``slack:C123``.  ``since`` is an ISO timestamp.
        Returns an ``InboundFetchResult`` with messages and high-water mark.
        """
        if not since:
            logger.warning(
                "fetch_inbound_since called without a cursor"
                " — reconciler should always provide one",
                channel_jid=channel_jid,
            )
            return InboundFetchResult(messages=[])
        if not self.owns_jid(channel_jid):
            return InboundFetchResult(messages=[])
        channel_id = channel_id_from_jid(channel_jid)
        # conversations.history `oldest` is inclusive (ts >= oldest), so add
        # a 1μs epsilon to make it exclusive and prevent the cursor from
        # stalling on the boundary message every reconciliation cycle.
        # IMPORTANT: Slack timestamps are "seconds.microseconds" (two
        # integers separated by a dot), NOT floats.  str(float) can produce
        # 7+ decimal digits which Slack misparses, shifting the decimal
        # point and creating a far-future timestamp that returns 0 results.
        dt = datetime.fromisoformat(since)
        total_us = int(dt.timestamp() * 1_000_000) + 1  # +1μs epsilon
        since_epoch = f"{total_us // 1_000_000}.{total_us % 1_000_000:06d}"
        messages, hwm = await self.history.fetch_missed_messages_with_watermark(
            channel_id, since_epoch
        )
        return InboundFetchResult(messages=messages, high_water_mark=hwm)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_user_name(self, user_id: str) -> str:
        """Look up a Slack user's display name, falling back to user ID.

        Results are cached for 1 hour so that a user sending multiple messages
        triggers a single users.info call rather than one per message.
        """
        cached = self._user_name_cache.get(user_id)
        if cached is not None:
            return cached
        if not self._app:
            return user_id
        try:
            resp = await self._app.client.users_info(user=user_id)
            user = resp.get("user", {})
            profile = user.get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user_id
            )
            self._user_name_cache.put(user_id, name)
        except Exception as exc:  # noqa: BLE001 - Slack user lookup failures fall back to the raw user ID.
            logger.debug("Failed to resolve Slack user name", user_id=user_id, error=str(exc))
            return user_id
        else:
            return name

    async def _resolve_channel_name(self, channel_id: str) -> str:
        """Look up a Slack channel name, falling back to channel ID.

        Results are cached for 1 hour to avoid redundant API calls.
        """
        cached = self._channel_name_cache.get(channel_id)
        if cached is not None:
            return cached
        if not self._app:
            return channel_id
        try:
            resp = await self._app.client.conversations_info(channel=channel_id)
            channel = resp.get("channel", {})
            name: str = channel.get("name", channel_id)
            self._channel_name_cache.put(channel_id, name)
        except Exception as exc:  # noqa: BLE001 - Slack channel lookup failures fall back to the channel ID.
            logger.debug(
                "Failed to resolve Slack channel name", channel_id=channel_id, error=str(exc)
            )
            return channel_id
        else:
            return name
