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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

from pynchy.logger import logger
from pynchy.plugins.channels.slack._blocks import SlackBlocksFormatter
from pynchy.types import InboundFetchResult, NewMessage, OutboundEvent

from ._allowlist import SlackAllowlist
from ._cache import TtlCache
from ._events import SlackEvents
from ._ids import JID_PREFIX, _channel_id_from_jid, _jid
from ._interactions import SlackInteractions
from ._lifecycle import SlackLifecycle
from ._ui import build_ask_user_blocks, normalize_chat_name, split_text


@dataclass(frozen=True)
class _SlackHistoryPage:
    messages: list[dict[str, Any]]
    has_more: bool


class SlackChannel:
    """Pynchy ``Channel`` protocol implementation backed by Slack Socket Mode."""

    prefix_assistant_name: bool = False  # Slack shows the bot username already

    def __init__(
        self,
        connection_name: str,
        bot_token: str,
        app_token: str,
        chat_names: list[str],
        allow_create: bool,
        on_message: Callable[[str, NewMessage], None],
        on_chat_metadata: Callable[[str, str, str | None], None],
        on_reaction: Callable[[str, str, str, str], None] | None = None,
        on_ask_user_answer: Callable[[str, dict[str, Any]], None] | None = None,
        on_approval_decision: Callable[[str, str, str, str], None] | None = None,
        on_agent_stop: Callable[[str, str], None] | None = None,
    ) -> None:
        self.name = connection_name
        self.formatter = SlackBlocksFormatter()
        self._connection_name = connection_name
        self._bot_token = bot_token
        self._app_token = app_token
        self._chat_names = {normalize_chat_name(name) for name in chat_names}
        self._allow_create = allow_create
        self._chat_name_to_id: dict[str, str] = {}
        self._allowed_channel_ids: set[str] = set()
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._on_reaction = on_reaction
        self._on_ask_user_answer = on_ask_user_answer
        # on_approval_decision(chat_jid, action, short_id, user_id)
        self._on_approval_decision = on_approval_decision
        # on_agent_stop(group_name, user_id)
        self._on_agent_stop = on_agent_stop
        self._connected = False
        self._shutting_down = False

        # Lazy-initialised in connect()
        self._app: Any = None
        self._handler: Any = None
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

    def _on_handler_done(self, task: asyncio.Task[None]) -> None:
        self.lifecycle._on_handler_done(task)

    async def _reconnect_with_backoff(self, delay: float = 5.0) -> None:
        await self.lifecycle._reconnect_with_backoff(delay)

    # ------------------------------------------------------------------
    # Allowlist — delegated to self.allowlist
    # ------------------------------------------------------------------

    async def create_group(self, name: str) -> str:
        return await self.allowlist.create_group(name)

    async def resolve_chat_jid(self, chat_name: str) -> str | None:
        return await self.allowlist.resolve_chat_jid(chat_name)

    def _register_allowed_channel(self, name: str, channel_id: str) -> None:
        self.allowlist._register_allowed_channel(name, channel_id)

    def _is_allowed_channel(self, channel_id: str) -> bool:
        return self.allowlist._is_allowed_channel(channel_id)

    # ------------------------------------------------------------------
    # Inbound events — delegated to self.events
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        self.events._register_handlers()

    async def _on_slack_message(self, event: dict[str, Any]) -> None:
        await self.events._on_slack_message(event)

    def _dedup_ts(self, ts: str) -> bool:
        return self.events._dedup_ts(ts)

    def _normalize_bot_mention(self, text: str) -> str:
        return self.events._normalize_bot_mention(text)

    # ------------------------------------------------------------------
    # Channel protocol
    # ------------------------------------------------------------------

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        """Render an outbound event and send it to the Slack channel."""
        if not self._app or not self.owns_jid(jid):
            return
        rendered = self.formatter.render(event)
        channel_id = _channel_id_from_jid(jid)
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
        channel_id = _channel_id_from_jid(jid)
        kwargs: dict[str, Any] = {"channel": channel_id, "text": rendered.text}
        if rendered.blocks:
            kwargs["blocks"] = rendered.blocks
        resp = await self._app.client.chat_postMessage(**kwargs)
        return cast("str | None", resp.get("ts"))

    async def update_event(self, jid: str, message_id: str, event: OutboundEvent) -> None:
        """Update an existing Slack message in-place with a rendered event."""
        if not self._app or not self.owns_jid(jid):
            return
        rendered = self.formatter.render(event)
        channel_id = _channel_id_from_jid(jid)
        kwargs: dict[str, Any] = {"channel": channel_id, "ts": message_id, "text": rendered.text}
        if rendered.blocks:
            kwargs["blocks"] = rendered.blocks
        await self._app.client.chat_update(**kwargs)

    def owns_jid(self, jid: str) -> bool:
        if not jid.startswith(JID_PREFIX):
            return False
        return self._is_allowed_channel(_channel_id_from_jid(jid))

    async def set_typing(self, jid: str, is_typing: bool) -> None:
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
        sender: str,
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
        channel_id = _channel_id_from_jid(jid)
        # Convert Unicode emoji to Slack name, or strip colons from name format
        emoji_name = self._UNICODE_TO_SLACK_NAME.get(emoji, emoji.strip(":"))
        try:
            await self._app.client.reactions_add(
                channel=channel_id, timestamp=slack_ts, name=emoji_name
            )
        except Exception as exc:
            logger.debug("Slack reaction failed", err=str(exc))

    async def send_ask_user(
        self, jid: str, request_id: str, questions: list[dict[str, Any]]
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
        channel_id = _channel_id_from_jid(jid)

        blocks = build_ask_user_blocks(request_id, questions)
        # Plain text for notifications / clients that don't render blocks
        fallback = "Question: " + "; ".join(q.get("question", "") for q in questions)

        resp = await self._app.client.chat_postMessage(
            channel=channel_id, blocks=blocks, text=fallback
        )
        return cast("str | None", resp.get("ts"))

    # ------------------------------------------------------------------
    # History catch-up (reconnect recovery)
    # ------------------------------------------------------------------

    @staticmethod
    def _history_high_water_mark(
        raw_messages: list[dict[str, Any]], current_high_water_mark: str
    ) -> tuple[str, str]:
        newest_ts = raw_messages[-1].get("ts", "")
        if not newest_ts:
            return current_high_water_mark, ""
        hwm_iso = datetime.fromtimestamp(float(newest_ts), tz=UTC).isoformat()
        if hwm_iso > current_high_water_mark:
            return hwm_iso, newest_ts
        return current_high_water_mark, newest_ts

    @staticmethod
    def _history_event_fields(event: dict[str, Any]) -> tuple[str, str, str] | None:
        if event.get("bot_id") or event.get("subtype"):
            return None
        user_id = event.get("user")
        text = event.get("text", "")
        ts = event.get("ts", "")
        if not user_id or not ts:
            return None
        return user_id, text, ts

    async def _history_new_message(
        self, channel_id: str, event: dict[str, Any]
    ) -> NewMessage | None:
        fields = self._history_event_fields(event)
        if fields is None:
            return None
        user_id, text, ts = fields
        sender_name = await self._resolve_user_name(user_id)
        return NewMessage(
            id=f"slack-{ts}",
            chat_jid=_jid(channel_id),
            sender=user_id,
            sender_name=sender_name,
            content=self._normalize_bot_mention(text),
            timestamp=datetime.fromtimestamp(float(ts), tz=UTC).isoformat(),
            is_from_me=False,
            metadata={"slack_ts": ts},
        )

    async def _history_user_messages(
        self, channel_id: str, raw_messages: list[dict[str, Any]]
    ) -> list[NewMessage]:
        results: list[NewMessage] = []
        for event in raw_messages:
            message = await self._history_new_message(channel_id, event)
            if message is not None:
                results.append(message)
        return results

    async def _history_page(
        self, channel_id: str, oldest: str, *, limit: int
    ) -> _SlackHistoryPage | None:
        try:
            resp = await self._app.client.conversations_history(
                channel=channel_id, oldest=oldest, limit=limit
            )
        except Exception:
            logger.warning("Failed to fetch Slack history for catch-up", channel=channel_id)
            return None
        raw_messages = list(cast("list[dict[str, Any]]", resp.get("messages", [])))
        raw_messages.reverse()
        return _SlackHistoryPage(messages=raw_messages, has_more=bool(resp.get("has_more")))

    @staticmethod
    def _should_continue_history_scan(
        *, newest_ts: str, current_oldest: str, has_more: bool, results: list[NewMessage]
    ) -> bool:
        if results or not has_more:
            return False
        return bool(newest_ts) and newest_ts != current_oldest

    async def _fetch_missed_messages_with_watermark(
        self, channel_id: str, oldest: str, *, limit: int = 1000
    ) -> tuple[list[NewMessage], str]:
        """Fetch messages via ``conversations.history`` with pagination.

        Returns ``(user_messages, high_water_mark)`` where *high_water_mark*
        is the ISO timestamp of the newest raw message seen (including bot
        messages).  The reconciler uses it to advance its cursor past
        bot-only windows.

        Paginates through bot-only pages (up to 10 pages) so that stale
        cursors buried under hundreds of bot messages can still reach
        recent user messages.
        """
        if not self._app:
            return [], ""
        if not self._is_allowed_channel(channel_id):
            return [], ""

        _MAX_PAGES = 10
        current_oldest = oldest
        high_water_mark = ""

        for _page in range(_MAX_PAGES):
            page = await self._history_page(channel_id, current_oldest, limit=limit)
            if page is None:
                return [], high_water_mark

            if not page.messages:
                return [], high_water_mark

            high_water_mark, newest_ts = self._history_high_water_mark(
                page.messages, high_water_mark
            )
            results = await self._history_user_messages(channel_id, page.messages)

            if not self._should_continue_history_scan(
                newest_ts=newest_ts,
                current_oldest=current_oldest,
                has_more=page.has_more,
                results=results,
            ):
                return results, high_water_mark

            logger.debug(
                "Skipping bot-only page in catch-up",
                channel=channel_id,
                page=_page,
                skipped_to=newest_ts,
            )
            current_oldest = newest_ts

        return [], high_water_mark

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
        channel_id = _channel_id_from_jid(channel_jid)
        # conversations.history `oldest` is inclusive (ts >= oldest), so add
        # a 1µs epsilon to make it exclusive and prevent the cursor from
        # stalling on the boundary message every reconciliation cycle.
        # IMPORTANT: Slack timestamps are "seconds.microseconds" (two
        # integers separated by a dot), NOT floats.  str(float) can produce
        # 7+ decimal digits which Slack misparses, shifting the decimal
        # point and creating a far-future timestamp that returns 0 results.
        dt = datetime.fromisoformat(since)
        total_us = int(dt.timestamp() * 1_000_000) + 1  # +1µs epsilon
        since_epoch = f"{total_us // 1_000_000}.{total_us % 1_000_000:06d}"
        messages, hwm = await self._fetch_missed_messages_with_watermark(channel_id, since_epoch)
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
            return name
        except Exception as exc:
            logger.debug("Failed to resolve Slack user name", user_id=user_id, error=str(exc))
            return user_id

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
            return name
        except Exception as exc:
            logger.debug(
                "Failed to resolve Slack channel name", channel_id=channel_id, error=str(exc)
            )
            return channel_id
