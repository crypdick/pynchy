"""Registered groups and workspace profiles."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any

from pynchy.state.connection import _get_db
from pynchy.types import (
    ContainerConfig,
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)

_CORRUPT_SECURITY_PROFILE_ERROR = (
    "Corrupt security_profile for workspace {folder!r}; "
    "refusing to load rather than silently defaulting to permissive trust"
)
_INVALID_WORKSPACE_PROFILE_ERROR = "Invalid workspace profile: {errors}"


def _row_to_workspace_profile(row: Row) -> WorkspaceProfile:
    """Convert database row to WorkspaceProfile."""
    container_config = None
    if row["container_config"]:
        container_config = ContainerConfig.from_dict(json.loads(row["container_config"]))

    security = WorkspaceSecurity()
    if row["security_profile"]:
        try:
            sec_data = json.loads(row["security_profile"])
            services = {
                svc_name: ServiceTrustConfig(
                    public_source=svc_data["public_source"],
                    secret_data=svc_data["secret_data"],
                    public_sink=svc_data["public_sink"],
                    dangerous_writes=svc_data["dangerous_writes"],
                )
                for svc_name, svc_data in sec_data["services"].items()
            }
            security = WorkspaceSecurity(
                services=services,
                contains_secrets=sec_data["contains_secrets"],
            )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(
                _CORRUPT_SECURITY_PROFILE_ERROR.format(folder=row["folder"])
            ) from exc

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
        raise ValueError(_INVALID_WORKSPACE_PROFILE_ERROR.format(errors="; ".join(errors)))

    db = _get_db()

    security_data = {
        "services": {
            svc_name: {
                "public_source": config.public_source,
                "secret_data": config.secret_data,
                "public_sink": config.public_sink,
                "dangerous_writes": config.dangerous_writes,
            }
            for svc_name, config in profile.security.services.items()
        },
        "contains_secrets": profile.security.contains_secrets,
    }

    await db.execute(
        """INSERT OR REPLACE INTO registered_groups -- temporal-ok
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


async def delete_workspace_profile(jid: str) -> None:
    """Delete a workspace profile by JID."""
    db = _get_db()
    await db.execute("DELETE FROM registered_groups WHERE jid = ?", (jid,))
    await db.commit()


async def get_all_workspace_profiles() -> dict[str, WorkspaceProfile]:
    """Get all workspace profiles as dict of jid -> WorkspaceProfile."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM registered_groups")
    rows = await cursor.fetchall()
    return {row["jid"]: _row_to_workspace_profile(row) for row in rows}
