"""Registered groups and workspace profiles."""

from __future__ import annotations

import json
from dataclasses import asdict

from pynchy.db._connection import _get_db
from pynchy.logger import logger
from pynchy.types import (
    ContainerConfig,
    ServiceTrustConfig,
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
            services = {}
            for svc_name, svc_data in sec_data.get("services", {}).items():
                services[svc_name] = ServiceTrustConfig(
                    public_source=svc_data.get("public_source", True),
                    secret_data=svc_data.get("secret_data", True),
                    public_sink=svc_data.get("public_sink", True),
                    dangerous_writes=svc_data.get("dangerous_writes", True),
                )

            security = WorkspaceSecurity(
                services=services,
                contains_secrets=sec_data.get("contains_secrets", False),
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
