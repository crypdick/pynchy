"""Registered groups and workspace profiles."""

from __future__ import annotations

import json
from dataclasses import asdict

from pynchy.db._connection import _get_db
from pynchy.logger import logger
from pynchy.types import (
    ContainerConfig,
    McpToolConfig,
    RateLimitConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)


def _row_to_workspace_profile(row) -> WorkspaceProfile:
    """Convert database row to WorkspaceProfile."""
    container_config = None
    if row["container_config"]:
        container_config = ContainerConfig.from_dict(json.loads(row["container_config"]))

    security = WorkspaceSecurity()
    try:
        if row["security_profile"]:
            sec_data = json.loads(row["security_profile"])
            mcp_tools = {}
            for tool_name, tool_data in sec_data.get("mcp_tools", {}).items():
                mcp_tools[tool_name] = McpToolConfig(
                    risk_tier=tool_data.get("risk_tier", "human-approval"),
                    enabled=tool_data.get("enabled", True),
                )

            rate_limits = None
            rl_data = sec_data.get("rate_limits")
            if rl_data is not None:
                rate_limits = RateLimitConfig(
                    max_calls_per_hour=rl_data.get("max_calls_per_hour", 500),
                    per_tool_overrides=rl_data.get("per_tool_overrides", {}),
                )

            security = WorkspaceSecurity(
                mcp_tools=mcp_tools,
                default_risk_tier=sec_data.get("default_risk_tier", "human-approval"),
                rate_limits=rate_limits,
                allow_filesystem_access=sec_data.get("allow_filesystem_access", True),
                allow_network_access=sec_data.get("allow_network_access", True),
            )
    except (KeyError, json.JSONDecodeError) as exc:
        logger.warning(
            "Failed to parse security profile, using defaults",
            folder=row["folder"],
            err=str(exc),
        )

    return WorkspaceProfile(
        jid=row["jid"],
        name=row["name"],
        folder=row["folder"],
        trigger=row["trigger_pattern"],
        container_config=container_config,
        security=security,
        is_admin=bool(row["is_admin"]),
        added_at=row["added_at"],
    )


async def get_workspace_profile(jid: str) -> WorkspaceProfile | None:
    """Get a workspace profile by JID."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM registered_groups WHERE jid = ?", (jid,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_workspace_profile(row)


async def set_workspace_profile(profile: WorkspaceProfile) -> None:
    """Register or update a workspace profile.

    Validates the profile before saving. Raises ValueError if validation fails.
    """
    errors = profile.validate()
    if errors:
        raise ValueError(f"Invalid workspace profile: {'; '.join(errors)}")

    db = _get_db()

    rl = profile.security.rate_limits
    security_data = {
        "mcp_tools": {
            tool_name: {"risk_tier": config.risk_tier, "enabled": config.enabled}
            for tool_name, config in profile.security.mcp_tools.items()
        },
        "default_risk_tier": profile.security.default_risk_tier,
        "rate_limits": {
            "max_calls_per_hour": rl.max_calls_per_hour,
            "per_tool_overrides": rl.per_tool_overrides,
        }
        if rl is not None
        else None,
        "allow_filesystem_access": profile.security.allow_filesystem_access,
        "allow_network_access": profile.security.allow_network_access,
    }

    await db.execute(
        """INSERT OR REPLACE INTO registered_groups
            (jid, name, folder, trigger_pattern, added_at,
             container_config, security_profile, is_admin)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            profile.jid,
            profile.name,
            profile.folder,
            profile.trigger,
            profile.added_at,
            json.dumps(asdict(profile.container_config)) if profile.container_config else None,
            json.dumps(security_data),
            1 if profile.is_admin else 0,
        ),
    )
    await db.commit()


async def get_all_workspace_profiles() -> dict[str, WorkspaceProfile]:
    """Get all workspace profiles as dict of jid -> WorkspaceProfile."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM registered_groups")
    rows = await cursor.fetchall()
    return {row["jid"]: _row_to_workspace_profile(row) for row in rows}
