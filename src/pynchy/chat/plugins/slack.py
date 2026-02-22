"""Built-in Slack channel plugin.

Connects to Slack via Socket Mode (bolt) and maps Slack channels/DMs to
pynchy workspaces.  Each Slack conversation is identified by a JID of the
form ``slack:<CHANNEL_ID>`` so it coexists with other channel plugins.

Activation: define ``[connection.slack.<name>]`` entries in config.toml and
provide token env var names (e.g. ``SLACK__BOT_TOKEN`` / ``SLACK__APP_TOKEN``).
The plugin returns ``None`` when no Slack connections are configured, so it
never interferes with installations that don't use Slack.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pluggy

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.types import NewMessage

hookimpl = pluggy.HookimplMarker("pynchy")

JID_PREFIX = "slack:"


def _jid(channel_id: str) -> str:
    """Convert a Slack channel ID to a pynchy JID."""
    return f"{JID_PREFIX}{channel_id}"


def _channel_id_from_jid(jid: str) -> str:
    """Extract the Slack channel ID from a pynchy JID."""
    return jid.removeprefix(JID_PREFIX)


class _TtlCache:
    """Bounded cache with per-entry TTL for Slack API lookups.

    Evicts expired entries lazily on get/put.  Hard-caps at ``max_size``
    entries to bound memory regardless of TTL.
    """

    def __init__(self, ttl_seconds: float = 3600, max_size: int = 500) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._data: dict[str, tuple[str, float]] = {}  # key → (value, expiry_mono)

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if time.monotonic() > expiry:
            del self._data[key]
            return None
        return value

    def put(self, key: str, value: str) -> None:
        if len(self._data) >= self._max_size:
            self._evict_expired()
        # If still at capacity after eviction, drop oldest entry
        if len(self._data) >= self._max_size:
            oldest_key = next(iter(self._data))
            del self._data[oldest_key]
        self._data[key] = (value, time.monotonic() + self._ttl)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        self._data = {k: v for k, v in self._data.items() if v[1] > now}


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
    ) -> None:
        self.name = connection_name
        self._connection_name = connection_name
        self._bot_token = bot_token
        self._app_token = app_token
        self._chat_names = {_normalize_chat_name(name) for name in chat_names}
        self._allow_create = allow_create
        self._chat_name_to_id: dict[str, str] = {}
        self._allowed_channel_ids: set[str] = set()
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._on_reaction = on_reaction
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
        self._user_name_cache = _TtlCache(ttl_seconds=3600, max_size=500)
        self._channel_name_cache = _TtlCache(ttl_seconds=3600, max_size=500)

    # ------------------------------------------------------------------
    # Channel protocol
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        self._app = AsyncApp(token=self._bot_token)

        # Cache bot user ID so we can strip self-mentions from inbound text
        try:
            auth = await self._app.client.auth_test()
            self._bot_user_id = auth.get("user_id", "")
        except Exception:
            logger.warning("Failed to resolve bot user ID (mention stripping disabled)")

        await self._sync_allowed_channels()
        self._register_handlers()

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        self._handler_task = asyncio.create_task(
            self._handler.start_async(), name="slack-socket-mode"
        )
        self._handler_task.add_done_callback(self._on_handler_done)
        self._connected = True
        logger.info(
            "Slack channel connected (Socket Mode)",
            connection=self._connection_name,
            bot_user_id=self._bot_user_id,
        )

    async def send_message(self, jid: str, text: str) -> None:
        if not self._app or not self.owns_jid(jid):
            return
        channel_id = _channel_id_from_jid(jid)
        # Slack block limit is 3000 chars per section; split long messages.
        chunks = _split_text(text, max_len=3000)
        for chunk in chunks:
            await self._app.client.chat_postMessage(channel=channel_id, text=chunk)

    def is_connected(self) -> bool:
        return self._connected and self._handler_task is not None and not self._handler_task.done()

    def owns_jid(self, jid: str) -> bool:
        if not jid.startswith(JID_PREFIX):
            return False
        return self._is_allowed_channel(_channel_id_from_jid(jid))

    async def disconnect(self) -> None:
        self._connected = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._handler:
            with contextlib.suppress(Exception):
                await self._handler.close_async()
        if self._handler_task and not self._handler_task.done():
            self._handler_task.cancel()
        logger.info("Slack channel disconnected", connection=self._connection_name)

    async def reconnect(self) -> None:
        """Force an immediate reconnect regardless of current state."""
        logger.info("Slack reconnecting (forced)", connection=self._connection_name)
        self._connected = False
        if self._handler:
            with contextlib.suppress(Exception):
                await self._handler.close_async()
        if self._handler_task and not self._handler_task.done():
            self._handler_task.cancel()
        self._handler = None
        self._handler_task = None
        await self.connect()

    # ------------------------------------------------------------------
    # Shutdown coordination
    # ------------------------------------------------------------------

    def prepare_shutdown(self) -> None:
        """Signal imminent shutdown — suppress reconnect attempts."""
        self._shutting_down = True

    # ------------------------------------------------------------------
    # Internal: reconnect on unexpected task exit
    # ------------------------------------------------------------------

    def _on_handler_done(self, task: asyncio.Task[None]) -> None:
        """Called when the Socket Mode handler task exits for any reason."""
        if not self._connected or self._shutting_down:
            return  # clean shutdown or imminent shutdown — don't reconnect
        exc = task.exception() if not task.cancelled() else None
        logger.warning(
            "Slack Socket Mode task exited unexpectedly — scheduling reconnect",
            connection=self._connection_name,
            exc=str(exc) if exc else "cancelled",
        )
        self._connected = False
        coro = self._reconnect_with_backoff()
        try:
            self._reconnect_task = task.get_loop().create_task(coro, name="slack-reconnect")
        except RuntimeError:
            # Event loop is shutting down — can't schedule reconnect.
            coro.close()
            logger.debug("Cannot schedule Slack reconnect — event loop closing")

    async def _reconnect_with_backoff(self, delay: float = 5.0) -> None:
        """Reconnect with exponential backoff, capped at 5 minutes."""
        await asyncio.sleep(delay)
        # Guard: if disconnect() was called while we slept, or another path
        # already reconnected, bail out — otherwise connect() will spawn
        # aiohttp tasks that disconnect() can't cancel (shutdown race).
        if self._connected or self._shutting_down:
            return
        logger.info("Slack attempting reconnect", connection=self._connection_name, delay=delay)
        try:
            self._handler = None
            self._handler_task = None
            await self.connect()
            self._reconnect_task = None
        except Exception as exc:
            logger.warning("Slack reconnect failed, will retry", delay=delay, exc=str(exc))
            self._connected = False
            next_delay = min(delay * 2, 300)
            coro = self._reconnect_with_backoff(next_delay)
            try:
                self._reconnect_task = asyncio.create_task(coro, name="slack-reconnect")
            except RuntimeError:
                coro.close()
                logger.debug("Cannot schedule Slack reconnect retry — event loop closing")

    # ------------------------------------------------------------------
    # Configured chat allowlist
    # ------------------------------------------------------------------

    def _register_allowed_channel(self, name: str, channel_id: str) -> None:
        normalized = _normalize_chat_name(name)
        self._chat_name_to_id[normalized] = channel_id
        self._allowed_channel_ids.add(channel_id)

    def _is_allowed_channel(self, channel_id: str) -> bool:
        if not self._allowed_channel_ids:
            return False
        return channel_id in self._allowed_channel_ids

    async def _ensure_joined(self, channel_id: str, name: str) -> None:
        if not self._app:
            return
        try:
            await self._app.client.conversations_join(channel=channel_id)
        except Exception as exc:
            logger.debug(
                "Failed to join Slack channel (may be private)",
                channel=name,
                err=str(exc),
            )

    async def _sync_allowed_channels(self) -> None:
        if not self._chat_names:
            logger.info("Slack connection has no configured chats", connection=self._connection_name)
            self._allowed_channel_ids = set()
            self._chat_name_to_id = {}
            return

        for name in sorted(self._chat_names):
            channel_id = await self._find_channel_by_name(name)
            if channel_id is None:
                if self._allow_create:
                    jid = await self.create_group(name)
                    channel_id = _channel_id_from_jid(jid)
                else:
                    logger.warning(
                        "Slack chat not found; skipping",
                        connection=self._connection_name,
                        chat=name,
                    )
                    continue
            await self._ensure_joined(channel_id, name)
            self._register_allowed_channel(name, channel_id)

        logger.info(
            "Slack chats configured",
            connection=self._connection_name,
            count=len(self._allowed_channel_ids),
        )

    async def resolve_chat_jid(self, chat_name: str) -> str | None:
        """Resolve a configured chat name to a Slack JID."""
        normalized = _normalize_chat_name(chat_name)
        if normalized in self._chat_name_to_id:
            return _jid(self._chat_name_to_id[normalized])

        channel_id = await self._find_channel_by_name(normalized)
        if channel_id is None:
            if self._allow_create:
                return await self.create_group(chat_name)
            return None

        await self._ensure_joined(channel_id, normalized)
        self._register_allowed_channel(normalized, channel_id)
        return _jid(channel_id)

    # ------------------------------------------------------------------
    # Optional protocol extensions
    # ------------------------------------------------------------------

    async def create_group(self, name: str) -> str:
        """Create a Slack channel and return its pynchy JID.

        If a channel with the same name already exists, reuses it instead of
        failing.  Requires the ``channels:manage`` (public) or ``groups:write``
        (private) OAuth scope on the bot token.
        """
        assert self._app is not None
        # Slack channel names: lowercase, no spaces, max 80 chars.
        slack_name = _normalize_chat_name(name)[:80]
        try:
            resp = await self._app.client.conversations_create(name=slack_name, is_private=False)
            channel_id = resp["channel"]["id"]
            logger.info("Created Slack channel", name=slack_name, channel_id=channel_id)
        except Exception as exc:
            if "name_taken" not in str(exc):
                raise
            # Channel already exists — look it up by name and reuse it.
            channel_id = await self._find_channel_by_name(slack_name)
            if channel_id is None:
                raise RuntimeError(
                    f"Slack channel '{slack_name}' exists but could not be found via API"
                ) from exc
            # Ensure the bot is a member so it receives events.
            # conversations.join is a no-op if already a member.
            try:
                await self._app.client.conversations_join(channel=channel_id)
            except Exception as join_exc:
                logger.warning(
                    "Failed to join existing Slack channel (events may not be received)",
                    channel=slack_name,
                    err=str(join_exc),
                )
            logger.info("Reusing existing Slack channel", name=slack_name, channel_id=channel_id)
        self._chat_names.add(slack_name)
        self._register_allowed_channel(slack_name, channel_id)
        return _jid(channel_id)

    async def _find_channel_by_name(self, name: str) -> str | None:
        """Find a Slack channel by name, returning its ID or None."""
        assert self._app is not None
        cursor = None
        while True:
            kwargs: dict[str, Any] = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await self._app.client.conversations_list(**kwargs)
            for ch in resp.get("channels", []):
                if ch.get("name") == name:
                    return ch["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return None

    async def set_typing(self, jid: str, is_typing: bool) -> None:  # noqa: ARG002
        """Slack doesn't have a user-level typing indicator API, so this is a no-op."""

    async def post_message(self, jid: str, text: str) -> str | None:
        """Post a message and return its ``ts`` (message ID) for later updates."""
        if not self._app or not self.owns_jid(jid):
            return None
        channel_id = _channel_id_from_jid(jid)
        resp = await self._app.client.chat_postMessage(channel=channel_id, text=text)
        return resp.get("ts")

    async def update_message(self, jid: str, message_id: str, text: str) -> None:
        """Update an existing Slack message in-place.

        Raises on failure so callers (e.g. finalize_stream_or_broadcast) can
        detect the error and fall back to send_message.
        """
        if not self._app or not self.owns_jid(jid):
            logger.warning("update_message skipped — JID not owned", jid=jid)
            return
        channel_id = _channel_id_from_jid(jid)
        chunks = _split_text(text, max_len=3000)
        await self._app.client.chat_update(channel=channel_id, ts=message_id, text=chunks[0])

    async def send_reaction(
        self,
        jid: str,
        message_id: str,
        sender: str,
        emoji: str,  # noqa: ARG002
    ) -> None:
        """Add a reaction to a Slack message.

        ``message_id`` should be a Slack message ``ts`` value.
        """
        if not self._app or not self.owns_jid(jid):
            return
        channel_id = _channel_id_from_jid(jid)
        # Normalize emoji name (strip colons if present)
        emoji_name = emoji.strip(":")
        try:
            await self._app.client.reactions_add(
                channel=channel_id, timestamp=message_id, name=emoji_name
            )
        except Exception as exc:
            logger.debug("Slack reaction failed", err=str(exc))

    # ------------------------------------------------------------------
    # History catch-up (reconnect recovery)
    # ------------------------------------------------------------------

    async def fetch_missed_messages(
        self, channel_id: str, oldest: str, *, limit: int = 200
    ) -> list[NewMessage]:
        """Fetch messages sent while disconnected via ``conversations.history``.

        Args:
            channel_id: Slack channel ID to query.
            oldest: Epoch timestamp string — only messages after this are returned.
            limit: Max messages to fetch (Slack cap: 1000).

        Returns a chronologically ordered list of ``NewMessage`` objects with
        deterministic IDs.  Bot messages and subtypes are filtered out.
        """
        if not self._app:
            return []
        if not self._is_allowed_channel(channel_id):
            return []
        try:
            resp = await self._app.client.conversations_history(
                channel=channel_id, oldest=oldest, limit=limit
            )
        except Exception:
            logger.warning("Failed to fetch Slack history for catch-up", channel=channel_id)
            return []

        raw_messages: list[dict] = resp.get("messages", [])
        # Slack returns newest-first; reverse for chronological order.
        raw_messages.reverse()

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
        return results

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> list[NewMessage]:
        """Fetch Slack messages newer than ``since`` for a single channel.

        The reconciler resolves JIDs before calling — ``channel_jid`` is a
        Slack-native JID like ``slack:C123``.  ``since`` is an ISO timestamp.
        Returns messages with ``chat_jid`` set to the given ``channel_jid``.
        """
        if not since:
            logger.warning(
                "fetch_inbound_since called without a cursor"
                " — reconciler should always provide one",
                channel_jid=channel_jid,
            )
            return []
        if not self.owns_jid(channel_jid):
            return []
        channel_id = _channel_id_from_jid(channel_jid)
        # conversations.history `oldest` is inclusive (ts >= oldest), so add
        # a 1µs epsilon to make it exclusive and prevent the cursor from
        # stalling on the boundary message every reconciliation cycle.
        since_epoch = str(datetime.fromisoformat(since).timestamp() + 1e-6)
        return await self.fetch_missed_messages(channel_id, since_epoch)

    # ------------------------------------------------------------------
    # Internal: Slack event handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        assert self._app is not None

        @self._app.event("message")
        async def _handle_message(event: dict[str, Any], say: Any) -> None:  # noqa: ARG001
            await self._on_slack_message(event)

        @self._app.event("app_mention")
        async def _handle_mention(event: dict[str, Any], say: Any) -> None:  # noqa: ARG001
            await self._on_slack_message(event)

        @self._app.event("reaction_added")
        async def _handle_reaction(event: dict[str, Any]) -> None:  # noqa: ARG001
            await self._on_slack_reaction(event)

        # --- Slack Assistant panel (sidebar DM experience) ---
        self._register_assistant_handlers()

    def _register_assistant_handlers(self) -> None:
        """Register Slack Assistant API handlers for the sidebar panel."""
        from slack_bolt.context.async_context import AsyncBoltContext
        from slack_bolt.middleware.assistant.async_assistant import AsyncAssistant

        assistant = AsyncAssistant()

        @assistant.thread_started
        async def _on_thread_started(
            say: Any,
            set_suggested_prompts: Any,
        ) -> None:
            await say("How can I help?")
            await set_suggested_prompts(
                prompts=[
                    {"title": "Status", "message": "What are you working on?"},
                    {"title": "Tasks", "message": "Show my scheduled tasks"},
                ],
            )

        @assistant.user_message
        async def _on_user_message(
            payload: dict[str, Any],
            context: AsyncBoltContext,
            set_status: Any,
        ) -> None:
            await set_status("thinking...")
            channel_id = context.channel_id
            user_id = payload.get("user", "")
            text = payload.get("text", "")
            ts = payload.get("ts", "")

            if not channel_id or not user_id:
                return
            if not self._is_allowed_channel(channel_id):
                return

            jid = _jid(channel_id)
            sender_name = await self._resolve_user_name(user_id)
            timestamp = datetime.now(UTC).isoformat()

            self._on_chat_metadata(jid, timestamp, f"assistant:{user_id}")

            msg = NewMessage(
                id=f"slack-assistant-{ts}",
                chat_jid=jid,
                sender=user_id,
                sender_name=sender_name,
                content=text,
                timestamp=timestamp,
                is_from_me=False,
                metadata={
                    "slack_ts": ts,
                    "slack_channel_type": "assistant",
                },
            )
            logger.info("Slack assistant message", user=user_id, text_len=len(text))
            self._on_message(jid, msg)

        self._app.use(assistant)

    def _normalize_bot_mention(self, text: str) -> str:
        """Replace the bot's ``<@BOT_ID>`` mention with the canonical trigger.

        Slack sends mentions as ``<@UBOTID>`` which is meaningless to the
        trigger pattern.  Replacing it with ``@AgentName`` preserves the
        trigger intent so the downstream pattern check (``^@AgentName\\b``)
        still matches.  If the mention appears mid-text, it's replaced
        inline rather than stripped so context is preserved.
        """
        if not self._bot_user_id:
            return text
        trigger = f"@{get_settings().agent.name}"
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>", trigger, text).strip()

    def _dedup_ts(self, ts: str) -> bool:
        """Return True if this ``ts`` was already seen (duplicate event).

        Keeps a bounded dict so memory doesn't grow without limit.
        """
        import time as _time

        now = _time.monotonic()
        if ts in self._seen_ts:
            return True
        # Evict old entries when the dict gets too large
        if len(self._seen_ts) >= self._seen_ts_max:
            cutoff = now - 120  # 2 minutes
            self._seen_ts = {k: v for k, v in self._seen_ts.items() if v > cutoff}
        self._seen_ts[ts] = now
        return False

    async def _on_slack_message(self, event: dict[str, Any]) -> None:
        """Route an inbound Slack event to the pynchy message callback."""
        # Ignore bot messages, edits, and deletions
        if event.get("bot_id") or event.get("subtype") in (
            "message_changed",
            "message_deleted",
        ):
            return

        channel_id = event.get("channel")
        user_id = event.get("user")
        text = event.get("text", "")
        ts = event.get("ts", "")

        if not channel_id or not user_id:
            return
        if not self._is_allowed_channel(channel_id):
            return

        # Deduplicate: Slack fires both `message` and `app_mention` events
        # for the same @mention message — skip the second one.
        if self._dedup_ts(ts):
            return

        jid = _jid(channel_id)

        # Replace the bot's Slack-native @mention with the canonical
        # trigger word so the downstream trigger pattern still matches.
        text = self._normalize_bot_mention(text)

        # Resolve display name (fall back to user ID)
        sender_name = await self._resolve_user_name(user_id)

        # Compute timestamp once for both metadata and message
        timestamp = datetime.now(UTC).isoformat()

        # Report chat metadata so workspace auto-register can pick it up
        chat_name = await self._resolve_channel_name(channel_id)
        self._on_chat_metadata(jid, timestamp, chat_name)

        msg = NewMessage(
            id=f"slack-{ts}",
            chat_jid=jid,
            sender=user_id,
            sender_name=sender_name,
            content=text,
            timestamp=timestamp,
            is_from_me=False,
            metadata={"slack_ts": ts, "slack_channel_type": event.get("channel_type", "")},
        )

        logger.info(
            "Slack inbound message",
            channel=channel_id,
            user=user_id,
            text_len=len(text),
        )
        self._on_message(jid, msg)

    async def _on_slack_reaction(self, event: dict[str, Any]) -> None:
        """Route an inbound Slack reaction to the pynchy reaction callback."""
        if not self._on_reaction:
            return

        user_id = event.get("user", "")
        reaction = event.get("reaction", "")
        item = event.get("item", {})
        channel_id = item.get("channel", "")
        message_ts = item.get("ts", "")

        if not channel_id or not user_id or not reaction:
            return
        if not self._is_allowed_channel(channel_id):
            return

        jid = _jid(channel_id)
        self._on_reaction(jid, message_ts, user_id, reaction)

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
        except Exception:
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
        except Exception:
            return channel_id


# ------------------------------------------------------------------
# Plugin entry point
# ------------------------------------------------------------------


class SlackChannelPlugin:
    """Built-in plugin that activates when Slack tokens are configured."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> list[SlackChannel] | None:
        s = get_settings()
        configs = s.connection.slack
        if not configs:
            logger.debug("Slack channel skipped — no connections configured")
            return None

        # Guard against None/incomplete context (e.g. in tests)
        if context is None:
            return None
        on_message = getattr(context, "on_message_callback", None)
        on_metadata = getattr(context, "on_chat_metadata_callback", None)
        if on_message is None or on_metadata is None:
            return None

        on_reaction = getattr(context, "on_reaction_callback", None)
        channels: list[SlackChannel] = []

        for name, cfg in configs.items():
            connection_name = f"connection.slack.{name}"
            bot_env = (cfg.bot_token_env or "").strip()
            app_env = (cfg.app_token_env or "").strip()
            if not bot_env or not app_env:
                logger.warning(
                    "Slack connection skipped — empty token env var name",
                    connection=connection_name,
                    bot_token_env=cfg.bot_token_env,
                    app_token_env=cfg.app_token_env,
                )
                continue
            bot_token = os.environ.get(bot_env, "")
            app_token = os.environ.get(app_env, "")
            chat_names = list(cfg.chat.keys())

            if not chat_names:
                logger.warning(
                    "Slack connection has no configured chats; skipping",
                    connection=connection_name,
                )
                continue

            if not bot_token or not app_token:
                logger.warning(
                    "Slack connection skipped — missing tokens",
                    connection=connection_name,
                    bot_token_env=bot_env,
                    app_token_env=app_env,
                )
                continue

            allow_create = s.command_center.connection == connection_name

            channels.append(
                SlackChannel(
                    connection_name=connection_name,
                    bot_token=bot_token,
                    app_token=app_token,
                    chat_names=chat_names,
                    allow_create=allow_create,
                    on_message=on_message,
                    on_chat_metadata=on_metadata,
                    on_reaction=on_reaction,
                )
            )

        return channels or None


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------


def _normalize_chat_name(name: str) -> str:
    """Normalize Slack channel name to the canonical slug form."""
    cleaned = name.strip()
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    return cleaned.lower().replace(" ", "-")


def _split_text(text: str, *, max_len: int = 3000) -> list[str]:
    """Split text into chunks respecting the Slack block size limit.

    Tries to break on newlines when possible.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Try to find a newline break point
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks
