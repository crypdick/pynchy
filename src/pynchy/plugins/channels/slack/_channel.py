"""SlackChannel — pynchy Channel protocol implementation backed by Slack Socket Mode.

Implementation is split across sibling mixins in this package:

- ``_lifecycle``: connect/disconnect/reconnect and reconnect-on-exit
- ``_allowlist``: configured chat allowlist, resolution, and creation
- ``_events``: inbound Slack event routing (messages, mentions, reactions)
- ``_interactions``: Block Kit interactive-callback handlers (ask_user, approvals, stop)

This module holds the composition root plus the outbound-facing protocol
methods, history catch-up, and name-resolution helpers used across mixins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from pynchy.logger import logger
from pynchy.plugins.channels.slack._blocks import SlackBlocksFormatter
from pynchy.types import InboundFetchResult, NewMessage, OutboundEvent

from ._allowlist import SlackAllowlistMixin
from ._cache import TtlCache
from ._events import SlackEventsMixin
from ._ids import JID_PREFIX, _channel_id_from_jid, _jid
from ._interactions import SlackInteractionMixin
from ._lifecycle import SlackLifecycleMixin
from ._ui import build_ask_user_blocks, normalize_chat_name, split_text


class SlackChannel(
    SlackLifecycleMixin,
    SlackAllowlistMixin,
    SlackEventsMixin,
    SlackInteractionMixin,
):
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
        on_ask_user_answer: Callable[[str, dict], None] | None = None,
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
        self._user_name_cache = TtlCache(ttl_seconds=3600, max_size=500)
        self._channel_name_cache = TtlCache(ttl_seconds=3600, max_size=500)

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
        return resp.get("ts")

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

    async def send_ask_user(self, jid: str, request_id: str, questions: list[dict]) -> str | None:
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
        # Fallback text for notifications / clients that don't render blocks
        fallback = "Question: " + "; ".join(q.get("question", "") for q in questions)

        resp = await self._app.client.chat_postMessage(
            channel=channel_id, blocks=blocks, text=fallback
        )
        return resp.get("ts")

    # ------------------------------------------------------------------
    # History catch-up (reconnect recovery)
    # ------------------------------------------------------------------

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
            try:
                resp = await self._app.client.conversations_history(
                    channel=channel_id, oldest=current_oldest, limit=limit
                )
            except Exception:
                logger.warning("Failed to fetch Slack history for catch-up", channel=channel_id)
                return [], high_water_mark

            raw_messages: list[dict] = resp.get("messages", [])
            if not raw_messages:
                return [], high_water_mark

            # Slack returns newest-first; reverse for chronological order.
            raw_messages.reverse()

            # Track the newest raw ts for the high-water mark.
            newest_ts = raw_messages[-1].get("ts", "")
            if newest_ts:
                hwm_iso = datetime.fromtimestamp(float(newest_ts), tz=UTC).isoformat()
                if hwm_iso > high_water_mark:
                    high_water_mark = hwm_iso

            results: list[NewMessage] = []
            for event in raw_messages:
                # Same filters as _on_slack_message
                if event.get("bot_id") or event.get("subtype"):
                    continue
                user_id = event.get("user")
                text = event.get("text", "")
                ts = event.get("ts", "")
                if not user_id or not ts:
                    continue

                text = self._normalize_bot_mention(text)
                sender_name = await self._resolve_user_name(user_id)
                timestamp = datetime.fromtimestamp(float(ts), tz=UTC).isoformat()

                results.append(
                    NewMessage(
                        id=f"slack-{ts}",
                        chat_jid=_jid(channel_id),
                        sender=user_id,
                        sender_name=sender_name,
                        content=text,
                        timestamp=timestamp,
                        is_from_me=False,
                        metadata={"slack_ts": ts},
                    )
                )

            if results or not resp.get("has_more"):
                return results, high_water_mark

            # Page was all bot messages and there's more — skip ahead.
            if not newest_ts or newest_ts == current_oldest:
                return results, high_water_mark  # safety: avoid infinite loop
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

        Results are cached for 1 hour to avoid redundant API calls — the same
        user sending multiple messages no longer triggers repeated users.info.
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
            name = channel.get("name", channel_id)
            self._channel_name_cache.put(channel_id, name)
            return name
        except Exception as exc:
            logger.debug(
                "Failed to resolve Slack channel name", channel_id=channel_id, error=str(exc)
            )
            return channel_id
