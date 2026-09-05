"""Workspace access helpers for composable profile config.

Workspace identity resolves through profiles. Sender gating comes from
connection-level channel security, not workspace/profile config.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pynchy.config.merge import merge_workspace_profiles
from pynchy.config.models import ChannelOverrideConfig, ConnectionConfig, OwnerConfig
from pynchy.config.refs import channel_platform_from_name
from pynchy.config.settings import get_settings
from pynchy.plugins.api import (
    NewMessage,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.workspace.api import (
    ResolvedWorkspaceConfig,  # noqa: TC001 - beartype resolves workspace policy annotations at runtime.
    WorkspaceProfile,  # noqa: TC001 - beartype resolves access annotations at runtime.
)

_KNOWN_CHANNEL_PLATFORMS = {"slack", "whatsapp", "discord"}
_CHANNEL_PLUGIN_NAME_ERROR = "channel_plugin_name must be a string or None"


def resolve_channel_config(
    workspace_name: str,
    channel_jid: str | None = None,
    channel_plugin_name: str | None = None,
) -> ResolvedWorkspaceConfig:
    """Return the composable profile resolution for a workspace."""
    del channel_jid, channel_plugin_name
    s = get_settings()
    resolved = s.resolved_workspace_config(workspace_name)
    if resolved is not None:
        return resolved
    return merge_workspace_profiles([])


# ---------------------------------------------------------------------------
# Allowed-user resolution
# ---------------------------------------------------------------------------


def resolve_allowed_users(
    raw_list: list[str],
    user_groups: dict[str, list[str]],
    owner_config: OwnerConfig,
    channel_plugin_name: str | None = None,
) -> set[str] | None:
    """Expand group references and "owner" into a flat set of user IDs.

    Returns None if "*" is in the list (meaning everyone is allowed).
    Otherwise returns the union of all resolved user IDs.

    Resolution rules:
    - "*" -> short-circuit, allow everyone (returns None)
    - "owner" -> resolved via OwnerConfig for the channel platform
    - strings containing ":" -> literal user refs (e.g., "slack:alice")
    - everything else -> group name lookup (recursive, with cycle detection)
    """
    if "*" in raw_list:
        return None  # Wildcard — everyone allowed

    resolver = _AllowedUsersResolver(
        user_groups=user_groups,
        owner_config=owner_config,
        channel_plugin_name=channel_plugin_name,
    )
    resolver.resolve(raw_list)
    return resolver.result


@dataclass
class _AllowedUsersResolver:
    """Resolve user references into a flat allowed-user set."""

    user_groups: dict[str, list[str]]
    owner_config: OwnerConfig
    channel_plugin_name: str | None
    result: set[str] = field(default_factory=set)
    seen: set[str] = field(default_factory=set)

    def resolve(self, entries: list[str]) -> None:
        for entry in entries:
            if entry == "owner":
                owner_id = _resolve_owner(self.owner_config, self.channel_plugin_name)
                if owner_id:
                    self.result.add(owner_id)
                continue
            if ":" in entry:
                # Literal user ID (e.g., "slack:U04ABC").
                self.result.add(entry)
                continue
            if entry in self.seen:
                continue  # Cycle detection.
            self.seen.add(entry)
            group_members = self.user_groups.get(entry)
            if group_members is not None:
                self.resolve(group_members)


def _resolve_owner(owner_config: OwnerConfig, channel_plugin_name: str | None) -> str | None:
    """Resolve the owner identity for a given channel platform."""
    platform = channel_platform_from_name(channel_plugin_name)
    if platform == "whatsapp":
        return "whatsapp:owner"  # Sentinel — checked via is_from_me at runtime
    if platform == "slack" and owner_config.slack:
        return f"slack:{owner_config.slack}"
    # For unknown platforms or when no owner is configured, return a generic sentinel
    # that the caller can check against
    if platform and owner_config.slack:
        # Default: try the slack owner for any platform with a configured owner
        return f"slack:{owner_config.slack}"
    return None


def filter_allowed_messages(
    messages: list[NewMessage],
    group: WorkspaceProfile,
    channel_plugin_name: str | None,
) -> list[NewMessage]:
    """Filter messages to only those from allowed senders.

    Admin groups bypass the filter entirely (return all messages unchanged).
    Uses the same resolve → filter logic as _route_incoming_group so that
    the reconciler and the main message loop apply identical sender gating.

    Args:
        messages: List of NewMessage objects to filter.
        group: WorkspaceProfile with at least ``is_admin`` and ``folder``.
        channel_plugin_name: Channel name for platform resolution (e.g. "slack").

    Returns:
        Filtered list — only messages from allowed senders.
    """
    if channel_plugin_name is not None and not isinstance(channel_plugin_name, str):
        raise TypeError(_CHANNEL_PLUGIN_NAME_ERROR)
    if group.is_admin:
        return messages

    if channel_plugin_name is None:
        return messages
    connection = get_settings().connections.get(channel_plugin_name)
    if connection is None:
        return messages
    security = _message_security(connection, messages)
    if security is None or security.allowed_users is None:
        return messages

    settings = get_settings()
    policy_channel_name = _policy_channel_name(channel_plugin_name, connection)
    resolved_users = resolve_allowed_users(
        security.allowed_users,
        settings.user_groups,
        OwnerConfig(),
        policy_channel_name,
    )
    return [
        msg
        for msg in messages
        if is_user_allowed(
            msg.sender,
            policy_channel_name,
            resolved_users,
            is_from_me=msg.is_from_me,
            sender_name=msg.sender_name,
        )
    ]


def _message_security(
    connection: ConnectionConfig, messages: list[NewMessage]
) -> ChannelOverrideConfig | None:
    if connection.type == "matrix":
        return None
    for msg in messages:
        if security := _chat_security(connection, msg.chat_jid):
            return security
    return connection.security


def _chat_security(connection: ConnectionConfig, chat_jid: str) -> ChannelOverrideConfig | None:
    for chat_name, chat_cfg in connection.chat.items():
        if not _chat_name_matches_jid(chat_name, chat_jid):
            continue
        security = chat_cfg.security
        if security is not None:
            return security
    return None


def _chat_name_matches_jid(chat_name: str, chat_jid: str) -> bool:
    lowered_name = chat_name.casefold()
    lowered_jid = chat_jid.casefold()
    if lowered_name == lowered_jid:
        return True
    if lowered_jid.startswith("slack:") and lowered_jid[6:] == lowered_name:
        return True
    return lowered_jid.endswith(f":{lowered_name}")


def _policy_channel_name(channel_plugin_name: str, connection: ConnectionConfig) -> str:
    platform = channel_platform_from_name(channel_plugin_name)
    if platform in _KNOWN_CHANNEL_PLATFORMS:
        return platform
    return connection.type


def is_user_allowed(
    sender: str,
    channel_plugin_name: str | None,
    resolved_users: set[str] | None,
    *,
    is_from_me: bool | None = None,
    sender_name: str | None = None,
) -> bool:
    """Check if a sender is allowed by the resolved allowed_users set.

    Args:
        sender: The sender's platform-specific ID
        channel_plugin_name: The channel plugin name (e.g., "whatsapp", "slack")
        resolved_users: The resolved set from resolve_allowed_users, or None for wildcard
        is_from_me: WhatsApp is_from_me flag for owner detection
        sender_name: Human display name from the channel, when available
    """
    if resolved_users is None:
        return True  # Wildcard — everyone allowed

    # WhatsApp owner check via is_from_me
    if is_from_me and "whatsapp:owner" in resolved_users:
        return True

    # Check literal sender ID
    platform = channel_platform_from_name(channel_plugin_name)
    if platform:
        qualified = f"{platform}:{sender}"
        if qualified in resolved_users:
            return True
        if sender_name and f"{platform}:{sender_name}".casefold() in {
            user.casefold() for user in resolved_users
        }:
            return True

    # Also check the raw sender (for pre-qualified IDs)
    return sender in resolved_users
