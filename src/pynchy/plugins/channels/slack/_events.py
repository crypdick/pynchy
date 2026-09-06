"""Inbound Slack event handling: message/mention/reaction routing, dedup.

A composed collaborator of :class:`SlackChannel` (not a mixin). The channel
constructs one of these and delegates its inbound-event methods to it. The
collaborator holds a back-reference to the channel: it registers handlers on
the live ``_app``, routes through the channel's callbacks, and reaches the
allowlist and interaction collaborators via ``self._channel``.
"""

from __future__ import annotations

import re
import time
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from re import Pattern
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from pynchy.logger import logger
from pynchy.plugins.api import NewMessage

from ._ids import jid as slack_jid
from ._ui import AGENT_STOP_ACTION_RE, ASK_USER_ACTION_RE, COP_APPROVAL_ACTION_RE

if TYPE_CHECKING:
    from slack_bolt.context.async_context import AsyncBoltContext

    from ._channel import SlackChannel
else:
    # beartype resolves the ``channel: SlackChannel`` forward ref at call time
    # from this module's globals. ``_channel`` imports this module, so a real
    # runtime import would be circular — bind a permissive substitute so the
    # forward ref resolves (mypy uses the real type from the branch above).
    SlackChannel = object


JsonDict = dict[str, object]


@runtime_checkable
class _SlackApp(Protocol):
    def event(
        self, _event_name: str
    ) -> Callable[[Callable[..., object]], Callable[..., object]]: ...

    def action(
        self, action_name: str | Pattern[str]
    ) -> Callable[[Callable[..., object]], Callable[..., object]]: ...

    def use(self, _middleware: object) -> object: ...


@runtime_checkable  # noqa: V102
class _SlackAssistant(Protocol):
    def thread_started(self, handler: Callable[..., object]) -> Callable[..., object]: ...

    def user_message(self, handler: Callable[..., object]) -> Callable[..., object]: ...


@dataclass(slots=True)
class SlackEvents:
    """Inbound event ingestion for :class:`SlackChannel`."""

    _channel: SlackChannel

    def _require_app(self) -> _SlackApp:
        return cast("_SlackApp", self._channel.require_slack_app())

    def register_handlers(self) -> None:
        self._register_handlers()

    def _register_handlers(self) -> None:
        ch = self._channel
        app = self._require_app()

        # slack_bolt is untyped to mypy (ignore_missing_imports), so the app
        # is ``Any`` and its ``.event()``/``.action()`` decorators register as
        # untyped — hence the per-handler ``untyped-decorator`` ignores below.

        @app.event("message")  # type: ignore[untyped-decorator]
        async def _handle_message(event: JsonDict, say: object) -> None:
            _ = say  # Slack Bolt supplies this callback argument.
            await ch.ingest_inbound_event(event)

        @app.event("app_mention")  # type: ignore[untyped-decorator]
        async def _handle_mention(event: JsonDict, say: object) -> None:
            _ = say  # Slack Bolt supplies this callback argument.
            await ch.ingest_inbound_event(event)

        @app.event("reaction_added")  # type: ignore[untyped-decorator]
        async def _handle_reaction(event: JsonDict) -> None:
            await self._on_slack_reaction(event)

        # --- ask_user interaction handlers (Block Kit buttons & text submit) ---
        @app.action(ASK_USER_ACTION_RE)  # type: ignore[untyped-decorator]
        async def _handle_ask_user_action(
            ack: Callable[[], Awaitable[object]],
            body: JsonDict,
            action: JsonDict,
        ) -> None:
            await ack()
            await ch.interactions.on_ask_user_interaction(body, action)

        # --- Approval button handlers (Approve/Deny from approval gate) ---
        @app.action(COP_APPROVAL_ACTION_RE)  # type: ignore[untyped-decorator]
        async def _handle_approval_action(
            ack: Callable[[], Awaitable[object]],
            body: JsonDict,
            action: JsonDict,
        ) -> None:
            await ack()
            await ch.interactions.on_approval_interaction(body, action)

        # --- Agent stop button handler ---
        @app.action(AGENT_STOP_ACTION_RE)  # type: ignore[untyped-decorator]
        async def _handle_agent_stop(
            ack: Callable[[], Awaitable[object]],
            body: JsonDict,
            action: JsonDict,
        ) -> None:
            await ack()
            await ch.interactions.on_agent_stop_interaction(body, action)

        # --- Slack Assistant panel (sidebar DM experience) ---
        self._register_assistant_handlers()

    def _register_assistant_handlers(self) -> None:
        """Register Slack Assistant API handlers for the sidebar panel."""
        from slack_bolt.middleware.assistant.async_assistant import (  # noqa: PLC0415 - optional Slack SDK loaded only when Slack connects.
            AsyncAssistant,
        )

        ch = self._channel
        assistant = cast("_SlackAssistant", AsyncAssistant())

        @assistant.thread_started  # type: ignore[untyped-decorator]
        async def _on_thread_started(
            say: Callable[[str], Awaitable[object]],
            set_suggested_prompts: Callable[..., Awaitable[object]],
        ) -> None:
            await say("How can I help?")
            await set_suggested_prompts(
                prompts=[
                    {"title": "Status", "message": "What are you working on?"},
                    {"title": "Tasks", "message": "Show my scheduled tasks"},
                ],
            )

        @assistant.user_message  # type: ignore[untyped-decorator]
        async def _on_user_message(
            payload: JsonDict,
            context: AsyncBoltContext,
            set_status: Callable[[str], Awaitable[object]],
        ) -> None:
            await set_status("thinking...")
            channel_id = context.channel_id
            user_id = cast("str", payload.get("user", ""))
            text = cast("str", payload.get("text", ""))
            ts = cast("str", payload.get("ts", ""))

            if not channel_id or not user_id:
                return
            if not ch.is_allowed_channel(channel_id):
                return

            jid = slack_jid(channel_id)
            sender_name = await ch.resolve_user_name(user_id)
            timestamp = datetime.now(UTC).isoformat()

            ch.emit_chat_metadata(jid, timestamp, f"assistant:{user_id}")

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
            ch.emit_message(jid, msg)

        app = self._require_app()
        app.use(assistant)

    def normalize_bot_mention(self, text: str) -> str:
        """Rewrite the bot's ``<@BOT_ID>`` mention as the canonical trigger.

        Slack sends mentions as ``<@UBOTID>`` which is meaningless to the
        trigger pattern.  Substituting ``@AgentName`` preserves the
        trigger intent so the downstream pattern check (``^@AgentName\\b``)
        still matches.  If the mention appears mid-text, it's substituted
        inline rather than stripped so context is preserved.
        """
        ch = self._channel
        if not ch.bot_user_id:
            return text
        trigger = f"@{ch.assistant_name}"
        return re.sub(rf"<@{re.escape(ch.bot_user_id)}>", trigger, text).strip()

    def dedup_ts(self, ts: str) -> bool:
        """Return True if this ``ts`` was already seen (duplicate event).

        Keeps a bounded dict so memory doesn't grow without limit.
        """
        ch = self._channel
        return ch.track_slack_ts(ts, time.monotonic())

    async def on_slack_message(self, event: JsonDict) -> None:
        """Route an inbound Slack event to the pynchy message callback."""
        ch = self._channel
        # Ignore bot messages, edits, and deletions
        if event.get("bot_id") or event.get("subtype") in (
            "message_changed",
            "message_deleted",
        ):
            return

        channel_id = cast("str", event.get("channel", ""))
        user_id = cast("str", event.get("user", ""))
        text = cast("str", event.get("text", ""))
        ts = cast("str", event.get("ts", ""))

        if not channel_id or not user_id:
            return
        if not ch.is_allowed_channel(channel_id):
            return

        # Deduplicate: Slack fires both `message` and `app_mention` events
        # for the same @mention message — skip the second one.
        if self.dedup_ts(ts):
            return

        jid = slack_jid(channel_id)

        # Rewrite the bot's Slack-native @mention as the canonical
        # trigger word so the downstream trigger pattern still matches.
        text = self.normalize_bot_mention(text)

        # Resolve display name (fall back to user ID)
        sender_name = await ch.resolve_user_name(user_id)

        # Compute timestamp once for both metadata and message
        timestamp = datetime.now(UTC).isoformat()

        # Report chat metadata so workspace auto-register can pick it up
        chat_name = await ch.resolve_channel_name(channel_id)
        ch.emit_chat_metadata(jid, timestamp, chat_name)

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
        ch.emit_message(jid, msg)

    async def _on_slack_reaction(self, event: JsonDict) -> None:
        """Route an inbound Slack reaction to the pynchy reaction callback."""
        ch = self._channel
        user_id = cast("str", event.get("user", ""))
        reaction = cast("str", event.get("reaction", ""))
        item = cast("JsonDict", event.get("item", {}))
        channel_id = cast("str", item.get("channel", ""))
        message_ts = cast("str", item.get("ts", ""))

        if not channel_id or not user_id or not reaction:
            return
        if not ch.is_allowed_channel(channel_id):
            return

        jid = slack_jid(channel_id)
        ch.emit_reaction(jid, message_ts, user_id, reaction)
