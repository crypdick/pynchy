"""Channel access resolution — walk the config cascade at runtime.

Resolves the effective access mode, trigger, trust, and allowed users
for a given workspace + channel combination by walking a 4-level cascade:

1. ``[workspace_defaults]``    (global defaults)
2. ``[workspaces.<name>]``     (workspace-level overrides)
3. ``channels.<plugin_name>``  (plugin-level channel override)
4. ``channels."<jid>"``        (JID-specific override, most specific)

At each level, non-None fields win over the previous layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pynchy.config import OwnerConfig, get_settings

if TYPE_CHECKING:
    from pynchy.types import ResolvedChannelConfig

# The fields that participate in the override cascade.  Adding a new
# overridable field means adding it here, to ChannelOverrideConfig,
# WorkspaceConfig, and WorkspaceDefaultsConfig — the helper below
# takes care of the rest.
_CASCADE_FIELDS = ("access", "mode", "trust", "trigger", "allowed_users")


def _apply_overrides(state: dict, source: object) -> None:
    """Apply non-None fields from *source* onto *state*.

    Works with WorkspaceConfig, ChannelOverrideConfig, or any object
    that has the standard cascade fields as attributes.
    """
    for field in _CASCADE_FIELDS:
        value = getattr(source, field, None)
        if value is not None:
            state[field] = value


def resolve_channel_config(
    workspace_name: str,
    channel_jid: str | None = None,
    channel_plugin_name: str | None = None,
) -> ResolvedChannelConfig:
    """Walk the resolution cascade and return a fully-resolved config.

    Cascade (most specific wins):
    1. workspaces.<name>.channels."<jid>"
    2. workspaces.<name>.channels.<plugin_name>
    3. workspaces.<name>.*
    4. workspace_defaults.*
    """
    from pynchy.types import ResolvedChannelConfig

    s = get_settings()
    defaults = s.workspace_defaults
    ws = s.workspaces.get(workspace_name)

    # Layer 0: global defaults
    state: dict = {
        "access": defaults.access,
        "mode": defaults.mode,
        "trust": defaults.trust,
        "trigger": defaults.trigger,
        "allowed_users": defaults.allowed_users or ["owner"],
    }

    # Layer 1: workspace-level overrides
    if ws is not None:
        _apply_overrides(state, ws)

        # Layer 2: per-channel overrides (plugin name, then specific JID)
        if ws.channels:
            if channel_plugin_name:
                ch_override = ws.channels.get(channel_plugin_name)
                if ch_override is not None:
                    _apply_overrides(state, ch_override)

            # JID-specific override (most specific, wins over plugin-level)
            if channel_jid:
                ch_override = ws.channels.get(channel_jid)
                if ch_override is not None:
                    _apply_overrides(state, ch_override)

    return ResolvedChannelConfig(**state)


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
    - strings containing ":" -> literal user IDs (e.g., "slack:U04ABC")
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
    if channel_plugin_name == "whatsapp":
        return "whatsapp:owner"  # Sentinel — checked via is_from_me at runtime
    if channel_plugin_name == "slack" and owner_config.slack:
        return f"slack:{owner_config.slack}"
    # For unknown platforms or when no owner is configured, return a generic sentinel
    # that the caller can check against
    if channel_plugin_name and owner_config.slack:
        # Default: try the slack owner for any platform with a configured owner
        return f"slack:{owner_config.slack}"
    return None


def is_user_allowed(
    sender: str,
    channel_plugin_name: str | None,
    resolved_users: set[str] | None,
    is_from_me: bool | None = None,
) -> bool:
    """Check if a sender is allowed by the resolved allowed_users set.

    Args:
        sender: The sender's platform-specific ID
        channel_plugin_name: The channel plugin name (e.g., "whatsapp", "slack")
        resolved_users: The resolved set from resolve_allowed_users, or None for wildcard
        is_from_me: WhatsApp is_from_me flag for owner detection
    """
    if resolved_users is None:
        return True  # Wildcard — everyone allowed

    # WhatsApp owner check via is_from_me
    if is_from_me and "whatsapp:owner" in resolved_users:
        return True

    # Check literal sender ID
    if channel_plugin_name:
        qualified = f"{channel_plugin_name}:{sender}"
        if qualified in resolved_users:
            return True

    # Also check the raw sender (for pre-qualified IDs)
    return sender in resolved_users
