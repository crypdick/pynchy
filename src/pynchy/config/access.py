"""Workspace access helpers for composable profile config.

Workspace identity resolves through profiles. Sender gating is permissive here
because connection/channel access policy lives outside this schema slice.
"""

from __future__ import annotations

from pynchy.config.merge import ResolvedWorkspaceConfig, merge_workspace_profiles
from pynchy.config.models import OwnerConfig
from pynchy.config.refs import channel_platform_from_name
from pynchy.config.settings import get_settings
from pynchy.types import NewMessage


def resolve_workspace_connection_name(workspace_name: str) -> str | None:
    """Return the owning connection name for a workspace, if configured.

    Current WorkspaceConfig carries profile selections only, so this layer has
    no connection reference to resolve.
    """
    assert workspace_name is not None
    return None


def resolve_channel_config(
    workspace_name: str,
    channel_jid: str | None = None,
    channel_plugin_name: str | None = None,
) -> ResolvedWorkspaceConfig:
    """Return the composable profile resolution for a workspace."""
    assert channel_jid is None or isinstance(channel_jid, str)
    assert channel_plugin_name is None or isinstance(channel_plugin_name, str)
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
    - strings containing ":" -> literal user refs (e.g., "slack:ricardo")
    - everything else -> group name lookup (recursive, with cycle detection)
    """
    if "*" in raw_list:
        return None  # Wildcard — everyone allowed

    result: set[str] = set()
    _resolve_into(raw_list, user_groups, owner_config, channel_plugin_name, result, seen=set())
    return result


def _resolve_into(
    entries: list[str],
    user_groups: dict[str, list[str]],
    owner_config: OwnerConfig,
    channel_plugin_name: str | None,
    result: set[str],
    seen: set[str],
) -> None:
    """Recursively resolve user entries into the result set."""
    for entry in entries:
        if entry == "*":
            # Shouldn't reach here (caller checks), but handle defensively
            return
        if entry == "owner":
            owner_id = _resolve_owner(owner_config, channel_plugin_name)
            if owner_id:
                result.add(owner_id)
            continue
        if ":" in entry:
            # Literal user ID (e.g., "slack:U04ABC")
            result.add(entry)
            continue
        # Group name lookup
        if entry in seen:
            continue  # Cycle detection
        seen.add(entry)
        group_members = user_groups.get(entry)
        if group_members is not None:
            _resolve_into(
                group_members, user_groups, owner_config, channel_plugin_name, result, seen
            )


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
    group: object,
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
    assert group is not None
    assert channel_plugin_name is None or isinstance(channel_plugin_name, str)
    return messages


def is_user_allowed(
    sender: str,
    channel_plugin_name: str | None,
    resolved_users: set[str] | None,
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
