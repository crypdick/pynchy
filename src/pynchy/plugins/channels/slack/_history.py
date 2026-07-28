"""Slack history catch-up helpers for reconnect recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from pynchy.logger import logger
from pynchy.plugins.api import NewMessage

from ._ids import jid as slack_jid

if TYPE_CHECKING:
    from ._channel import JsonDict, SlackChannel
else:
    SlackChannel = object
    JsonDict = dict[str, object]


@dataclass(frozen=True)
class _SlackHistoryPage:
    messages: list[JsonDict]
    has_more: bool


class SlackHistory:
    """Fetch missed Slack messages after reconnects."""

    def __init__(self, channel: SlackChannel) -> None:
        self._channel = channel

    @staticmethod
    def _high_water_mark(
        raw_messages: list[JsonDict], current_high_water_mark: str
    ) -> tuple[str, str]:
        newest_ts = str(raw_messages[-1].get("ts", ""))
        if not newest_ts:
            return current_high_water_mark, ""
        hwm_iso = datetime.fromtimestamp(float(newest_ts), tz=UTC).isoformat()
        if hwm_iso > current_high_water_mark:
            return hwm_iso, newest_ts
        return current_high_water_mark, newest_ts

    @staticmethod
    def _event_fields(event: JsonDict) -> tuple[str, str, str] | None:
        if event.get("bot_id") or event.get("subtype"):
            return None
        user_id = event.get("user")
        text = str(event.get("text", ""))
        ts = event.get("ts", "")
        if not isinstance(user_id, str) or not isinstance(ts, str) or not user_id or not ts:
            return None
        return user_id, text, ts

    async def _new_message(self, channel_id: str, event: JsonDict) -> NewMessage | None:
        fields = self._event_fields(event)
        if fields is None:
            return None
        user_id, text, ts = fields
        ch = self._channel
        sender_name = await ch.resolve_user_name(user_id)
        return NewMessage(
            id=f"slack-{ts}",
            chat_jid=slack_jid(channel_id),
            sender=user_id,
            sender_name=sender_name,
            content=ch.events.normalize_bot_mention(text),
            timestamp=datetime.fromtimestamp(float(ts), tz=UTC).isoformat(),
            is_from_me=False,
            metadata={"slack_ts": ts},
        )

    async def _user_messages(
        self, channel_id: str, raw_messages: list[JsonDict]
    ) -> list[NewMessage]:
        results: list[NewMessage] = []
        for event in raw_messages:
            message = await self._new_message(channel_id, event)
            if message is not None:
                results.append(message)
        return results

    async def _page(self, channel_id: str, oldest: str, *, limit: int) -> _SlackHistoryPage | None:
        ch = self._channel
        try:
            resp = await ch.require_slack_app().client.conversations_history(
                channel=channel_id, oldest=oldest, limit=limit
            )
        except Exception:  # noqa: BLE001 - history catch-up is best-effort and should not block reconnect.
            logger.warning("Failed to fetch Slack history for catch-up", channel=channel_id)
            return None
        raw_messages = list(cast("list[JsonDict]", resp.get("messages", [])))
        raw_messages.reverse()
        return _SlackHistoryPage(messages=raw_messages, has_more=bool(resp.get("has_more")))

    @staticmethod
    def _should_continue_scan(
        *, newest_ts: str, current_oldest: str, has_more: bool, results: list[NewMessage]
    ) -> bool:
        if results or not has_more:
            return False
        return bool(newest_ts) and newest_ts != current_oldest

    async def fetch_missed_messages_with_watermark(
        self, channel_id: str, oldest: str, *, limit: int = 1000
    ) -> tuple[list[NewMessage], str]:
        if not self._channel.slack_app:
            return [], ""
        if not self._channel.is_allowed_channel(channel_id):
            return [], ""

        max_pages = 10
        current_oldest = oldest
        high_water_mark = ""

        for page_index in range(max_pages):
            page = await self._page(channel_id, current_oldest, limit=limit)
            if page is None:
                return [], high_water_mark
            if not page.messages:
                return [], high_water_mark

            high_water_mark, newest_ts = self._high_water_mark(page.messages, high_water_mark)
            results = await self._user_messages(channel_id, page.messages)

            if not self._should_continue_scan(
                newest_ts=newest_ts,
                current_oldest=current_oldest,
                has_more=page.has_more,
                results=results,
            ):
                return results, high_water_mark

            logger.debug(
                "Skipping bot-only page in catch-up",
                channel=channel_id,
                page=page_index,
                skipped_to=newest_ts,
            )
            current_oldest = newest_ts

        return [], high_water_mark
