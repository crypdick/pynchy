"""Built-in Slack channel plugin.

Connects to Slack via Socket Mode (bolt) and maps Slack channels/DMs to
pynchy workspaces.  Each Slack conversation is identified by a JID of the
form ``slack:<CHANNEL_ID>`` so it coexists with other channel plugins.

Activation: set ``[slack]`` tokens in config.toml (or env vars
``SLACK__BOT_TOKEN`` / ``SLACK__APP_TOKEN``).  The plugin returns ``None``
when tokens are absent, so it never interferes with installations that
don't use Slack.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pluggy

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.types import NewMessage
from pynchy.utils import generate_message_id

hookimpl = pluggy.HookimplMarker("pynchy")

JID_PREFIX = "slack:"


def _jid(channel_id: str) -> str:
    """Convert a Slack channel ID to a pynchy JID."""
    return f"{JID_PREFIX}{channel_id}"


def _channel_id_from_jid(jid: str) -> str:
    """Extract the Slack channel ID from a pynchy JID."""
    return jid.removeprefix(JID_PREFIX)


class SlackChannel:
    """Pynchy ``Channel`` protocol implementation backed by Slack Socket Mode."""

    name: str = "slack"
    prefix_assistant_name: bool = False  # Slack shows the bot username already

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        on_message: Callable[[str, NewMessage], None],
        on_chat_metadata: Callable[[str, str, str | None], None],
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._connected = False

        # Lazy-initialised in connect()
        self._app: Any = None
        self._handler: Any = None
        self._handler_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Channel protocol
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        self._app = AsyncApp(token=self._bot_token)
        self._register_handlers()

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        self._handler_task = asyncio.create_task(
            self._handler.start_async(), name="slack-socket-mode"
        )
        self._connected = True
        logger.info("Slack channel connected (Socket Mode)")

    async def send_message(self, jid: str, text: str) -> None:
        if not self._app or not self.owns_jid(jid):
            return
        channel_id = _channel_id_from_jid(jid)
        # Slack block limit is 3000 chars per section; split long messages.
        chunks = _split_text(text, max_len=3000)
        for chunk in chunks:
            await self._app.client.chat_postMessage(channel=channel_id, text=chunk)

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith(JID_PREFIX)

    async def disconnect(self) -> None:
        self._connected = False
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception:
                logger.debug("Slack handler close error (ignored)")
        if self._handler_task and not self._handler_task.done():
            self._handler_task.cancel()
        logger.info("Slack channel disconnected")

    # ------------------------------------------------------------------
    # Optional protocol extensions
    # ------------------------------------------------------------------

    async def set_typing(self, jid: str, is_typing: bool) -> None:  # noqa: ARG002
        """Slack doesn't have a user-level typing indicator API, so this is a no-op."""

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

        jid = _jid(channel_id)

        # Resolve display name (fall back to user ID)
        sender_name = await self._resolve_user_name(user_id)

        # Compute timestamp once for both metadata and message
        timestamp = datetime.now(UTC).isoformat()

        # Report chat metadata so workspace auto-register can pick it up
        chat_name = await self._resolve_channel_name(channel_id)
        self._on_chat_metadata(jid, timestamp, chat_name)

        msg = NewMessage(
            id=generate_message_id("slack"),
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_user_name(self, user_id: str) -> str:
        """Look up a Slack user's display name, falling back to user ID."""
        if not self._app:
            return user_id
        try:
            resp = await self._app.client.users_info(user=user_id)
            user = resp.get("user", {})
            profile = user.get("profile", {})
            return (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user_id
            )
        except Exception:
            return user_id

    async def _resolve_channel_name(self, channel_id: str) -> str:
        """Look up a Slack channel name, falling back to channel ID."""
        if not self._app:
            return channel_id
        try:
            resp = await self._app.client.conversations_info(channel=channel_id)
            channel = resp.get("channel", {})
            return channel.get("name", channel_id)
        except Exception:
            return channel_id


# ------------------------------------------------------------------
# Plugin entry point
# ------------------------------------------------------------------


class SlackChannelPlugin:
    """Built-in plugin that activates when Slack tokens are configured."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> SlackChannel | None:
        cfg = get_settings().slack
        bot_token = cfg.bot_token.get_secret_value() if cfg.bot_token else ""
        app_token = cfg.app_token.get_secret_value() if cfg.app_token else ""

        if not bot_token or not app_token:
            logger.debug("Slack channel skipped — no tokens configured")
            return None

        # Guard against None/incomplete context (e.g. in tests)
        if context is None:
            return None
        on_message = getattr(context, "on_message_callback", None)
        on_metadata = getattr(context, "on_chat_metadata_callback", None)
        if on_message is None or on_metadata is None:
            return None

        return SlackChannel(
            bot_token=bot_token,
            app_token=app_token,
            on_message=on_message,
            on_chat_metadata=on_metadata,
        )


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------


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
