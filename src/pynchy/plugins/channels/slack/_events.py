"""Inbound Slack event handling: message/mention/reaction routing, dedup.

Split from ``_channel.py`` as a mixin so the channel module stays focused on
transport and lifecycle.  :class:`SlackChannel` mixes this in; every handler
uses the channel's own state (``self._app``, ``self._is_allowed_channel``,
``self._resolve_user_name``/``self._resolve_channel_name``, and the
``self._on_*`` callbacks), so the split is behavior-preserving.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.types import NewMessage

from ._ids import _jid
from ._ui import AGENT_STOP_ACTION_RE, ASK_USER_ACTION_RE, COP_APPROVAL_ACTION_RE


class SlackEventsMixin:
    """Inbound event ingestion for :class:`SlackChannel`."""

    def _register_handlers(self) -> None:
        assert self._app is not None

        @self._app.event("message")
        async def _handle_message(event: dict[str, Any], say: Any) -> None:
            await self._on_slack_message(event)

        @self._app.event("app_mention")
        async def _handle_mention(event: dict[str, Any], say: Any) -> None:
            await self._on_slack_message(event)

        @self._app.event("reaction_added")
        async def _handle_reaction(event: dict[str, Any]) -> None:
            await self._on_slack_reaction(event)

        # --- ask_user interaction handlers (Block Kit buttons & text submit) ---
        @self._app.action(ASK_USER_ACTION_RE)
        async def _handle_ask_user_action(
            ack: Any, body: dict[str, Any], action: dict[str, Any]
        ) -> None:
            await ack()
            await self._on_ask_user_interaction(body, action)

        # --- Approval button handlers (Approve/Deny from approval gate) ---
        @self._app.action(COP_APPROVAL_ACTION_RE)
        async def _handle_approval_action(
            ack: Any, body: dict[str, Any], action: dict[str, Any]
        ) -> None:
            await ack()
            await self._on_approval_interaction(body, action)

        # --- Agent stop button handler ---
        @self._app.action(AGENT_STOP_ACTION_RE)
        async def _handle_agent_stop(
            ack: Any, body: dict[str, Any], action: dict[str, Any]
        ) -> None:
            await ack()
            await self._on_agent_stop_interaction(body, action)

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
        """Rewrite the bot's ``<@BOT_ID>`` mention as the canonical trigger.

        Slack sends mentions as ``<@UBOTID>`` which is meaningless to the
        trigger pattern.  Substituting ``@AgentName`` preserves the
        trigger intent so the downstream pattern check (``^@AgentName\\b``)
        still matches.  If the mention appears mid-text, it's substituted
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
        now = time.monotonic()
        if ts in self._seen_ts:
            return True
        # Evict stale entries when the dict gets too large
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

        # Rewrite the bot's Slack-native @mention as the canonical
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
