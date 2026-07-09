"""Configured chat allowlist: resolving, joining, and creating Slack channels.

A composed collaborator of :class:`SlackChannel` (not a mixin). The channel
constructs one of these and delegates its allowlist-related methods to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pynchy.logger import logger

from ._ids import channel_id_from_jid
from ._ids import jid as slack_jid
from ._ui import normalize_chat_name

if TYPE_CHECKING:
    from ._channel import SlackChannel
else:
    # beartype resolves the ``channel: SlackChannel`` forward ref at call time
    # from this module's globals. ``_channel`` imports this module, so a real
    # runtime import would be circular — bind a permissive substitute so the
    # forward ref resolves (mypy uses the real type from the branch above).
    SlackChannel = object


class SlackAllowlist:
    """Chat allowlist resolution and channel creation for :class:`SlackChannel`."""

    def __init__(self, channel: SlackChannel) -> None:
        self._channel = channel

    def _require_app(self) -> Any:
        ch = self._channel
        app = ch.slack_app
        if app is None:
            raise RuntimeError("Slack app is not initialized")
        return app

    def register_allowed_channel(self, name: str, channel_id: str) -> None:
        self._channel.register_allowed_channel(name, channel_id)

    def _register_allowed_channel(self, name: str, channel_id: str) -> None:
        self.register_allowed_channel(name, channel_id)

    def is_allowed_channel(self, channel_id: str) -> bool:
        return self._channel.is_allowed_channel(channel_id)

    def _is_allowed_channel(self, channel_id: str) -> bool:
        return self.is_allowed_channel(channel_id)

    async def _ensure_joined(self, channel_id: str, name: str) -> None:
        app = self._require_app()
        try:
            await app.client.conversations_join(channel=channel_id)
        except Exception as exc:  # noqa: BLE001, RUF100 - Slack API join failures are best-effort for optional channels.
            logger.debug(
                "Failed to join Slack channel (may be private)",
                channel=name,
                err=str(exc),
            )

    async def sync_allowed_channels(self) -> None:
        ch = self._channel
        configured_chat_names = ch.configured_chat_names
        if not configured_chat_names:
            logger.info("Slack connection has no configured chats", connection=ch.connection_name)
            ch.clear_allowed_channels()
            return

        for name in sorted(configured_chat_names):
            channel_id = await self._find_channel_by_name(name)
            if channel_id is None:
                if ch.allow_create:
                    jid = await self.create_group(name)
                    channel_id = channel_id_from_jid(jid)
                else:
                    logger.warning(
                        "Slack chat not found; skipping",
                        connection=ch.connection_name,
                        chat=name,
                    )
                    continue
            await self._ensure_joined(channel_id, name)
            self.register_allowed_channel(name, channel_id)

        logger.info(
            "Slack chats configured",
            connection=ch.connection_name,
            count=ch.allowed_channel_count(),
        )

    async def _sync_allowed_channels(self) -> None:
        await self.sync_allowed_channels()

    async def resolve_chat_jid(self, chat_name: str) -> str | None:
        """Resolve a configured chat name to a Slack JID."""
        ch = self._channel
        normalized = normalize_chat_name(chat_name)
        if channel_id := ch.allowed_channel_id_for_name(normalized):
            return slack_jid(channel_id)

        channel_id = await self._find_channel_by_name(normalized)
        if channel_id is None:
            if ch.allow_create:
                return await self.create_group(chat_name)
            return None

        await self._ensure_joined(channel_id, normalized)
        self.register_allowed_channel(normalized, channel_id)
        return slack_jid(channel_id)

    async def create_group(self, name: str) -> str:
        """Create a Slack channel and return its pynchy JID.

        If a channel with the same name already exists, reuses it instead of
        failing.  Requires the ``channels:manage`` (public) or ``groups:write``
        (private) OAuth scope on the bot token.
        """
        ch = self._channel
        app = self._require_app()
        # Slack channel names: lowercase, no spaces, max 80 chars.
        slack_name = normalize_chat_name(name)[:80]
        try:
            resp = await app.client.conversations_create(name=slack_name, is_private=False)
            channel_id = resp["channel"]["id"]
            logger.info("Created Slack channel", name=slack_name, channel_id=channel_id)
        except Exception as exc:  # noqa: BLE001, RUF100 - Slack channel creation/reuse is a best-effort integration boundary.
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
                await app.client.conversations_join(channel=channel_id)
            except Exception as join_exc:  # noqa: BLE001, RUF100 - join retry is optional after name_taken reuse.
                logger.warning(
                    "Failed to join existing Slack channel (events may not be received)",
                    channel=slack_name,
                    err=str(join_exc),
                )
            logger.info("Reusing existing Slack channel", name=slack_name, channel_id=channel_id)
        ch.add_configured_chat_name(slack_name)
        self.register_allowed_channel(slack_name, channel_id)
        return slack_jid(channel_id)

    async def _find_channel_by_name(self, name: str) -> str | None:
        """Find a Slack channel by name, returning its ID or None."""
        app = self._require_app()
        cursor = None
        while True:
            kwargs: dict[str, Any] = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await app.client.conversations_list(**kwargs)
            for chan in resp.get("channels", []):
                if chan.get("name") == name:
                    return cast("str", chan["id"])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return None
