"""Sender-policy filtering for durable inbound message batches."""

from __future__ import annotations

from typing import Protocol

from pynchy.logger import logger
from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves sender policy annotations.
    Channel,
    NewMessage,
)
from pynchy.state.api import get_messages_since
from pynchy.workspace.api import (  # noqa: TC001 - beartype resolves sender policy annotations at runtime.
    WorkspaceProfile,
)


class SenderPolicyDeps(Protocol):
    @property
    def channels(self) -> list[Channel]: ...

    def filter_allowed_messages(
        self,
        messages: list[NewMessage],
        group: WorkspaceProfile,
        channel_plugin_name: str | None,
    ) -> list[NewMessage]: ...


def _channel_plugin_name(deps: SenderPolicyDeps, group_jid: str) -> str | None:
    return next((channel.name for channel in deps.channels if channel.owns_jid(group_jid)), None)


def allowed_group_messages(
    deps: SenderPolicyDeps,
    group_jid: str,
    group: WorkspaceProfile,
    messages: list[NewMessage],
) -> list[NewMessage]:
    """Apply channel sender policy while preserving authenticated external routes."""
    channel_plugin_name = _channel_plugin_name(deps, group_jid)
    authenticated_external_ids = {
        message.id
        for message in messages
        if (message.metadata or {}).get("authenticated_external_route") is True
    }
    channel_messages = [
        message for message in messages if message.id not in authenticated_external_ids
    ]
    allowed_channel_ids = {
        message.id
        for message in deps.filter_allowed_messages(channel_messages, group, channel_plugin_name)
    }
    # Marker bypasses only control channel sender allowlist. Provider body
    # remains untrusted and taints agent invocation.
    filtered_messages = [
        message
        for message in messages
        if message.id in authenticated_external_ids or message.id in allowed_channel_ids
    ]
    if not filtered_messages:
        logger.info("route_trace", step="skip_all_filtered", group=group.name)
    return filtered_messages


async def load_allowed_group_messages(
    deps: SenderPolicyDeps,
    group_jid: str,
    group: WorkspaceProfile,
    cursor: str,
) -> list[NewMessage]:
    """Load one durable pending batch through sender policy."""
    return allowed_group_messages(
        deps,
        group_jid,
        group,
        await get_messages_since(group_jid, cursor),
    )
