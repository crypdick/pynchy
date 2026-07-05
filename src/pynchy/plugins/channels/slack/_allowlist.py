"""Configured chat allowlist: resolving, joining, and creating Slack channels.

Split from ``_channel.py`` as a mixin so the channel module stays focused on
transport and message handling.  :class:`SlackChannel` mixes this in; every
method uses the channel's own state (``self._app``, ``self._chat_names``,
``self._chat_name_to_id``, ``self._allowed_channel_ids``), so the split is
behavior-preserving.
"""

from __future__ import annotations

from typing import Any

from pynchy.logger import logger

from ._ids import _channel_id_from_jid, _jid
from ._ui import normalize_chat_name


class SlackAllowlistMixin:
    """Chat allowlist resolution and channel creation for :class:`SlackChannel`."""

    def _register_allowed_channel(self, name: str, channel_id: str) -> None:
        normalized = normalize_chat_name(name)
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
            logger.info(
                "Slack connection has no configured chats", connection=self._connection_name
            )
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
        normalized = normalize_chat_name(chat_name)
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

    async def create_group(self, name: str) -> str:
        """Create a Slack channel and return its pynchy JID.

        If a channel with the same name already exists, reuses it instead of
        failing.  Requires the ``channels:manage`` (public) or ``groups:write``
        (private) OAuth scope on the bot token.
        """
        assert self._app is not None
        # Slack channel names: lowercase, no spaces, max 80 chars.
        slack_name = normalize_chat_name(name)[:80]
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
