"""Registered groups and workspace profiles."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiosqlite import Connection, Row
else:
    Connection = Any
    Row = Any

from pynchy.state.connection import _get_db, atomic_write
from pynchy.workspace.api import (
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
                cop_active=sec_data.get("cop_active", True),
            )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(_CORRUPT_SECURITY_PROFILE_ERROR.format(folder=row["folder"])) from exc

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

    security_data = _security_data(profile)

    async with atomic_write() as db:
        jid_cursor = await db.execute(
            "SELECT folder FROM registered_groups WHERE jid = ?",
            (profile.jid,),
        )
        jid_row = await jid_cursor.fetchone()
        if jid_row is not None and jid_row["folder"] != profile.folder:
            raise ValueError(
                f"Chat JID {profile.jid!r} is already owned by workspace {jid_row['folder']!r}"
            )
        folder_cursor = await db.execute(
            "SELECT jid FROM registered_groups WHERE folder = ?",
            (profile.folder,),
        )
        folder_row = await folder_cursor.fetchone()
        if folder_row is not None and folder_row["jid"] != profile.jid:
            raise ValueError(
                f"Workspace {profile.folder!r} is already bound to chat JID "
                f"{folder_row['jid']!r}; use explicit rebind"
            )
        await _upsert_workspace_profile(db, profile, security_data)


async def rebind_workspace_profile(profile: WorkspaceProfile) -> str | None:
    """Atomically move one workspace's JID after rejecting foreign ownership."""
    errors = profile.validate()
    if errors:
        raise ValueError(_INVALID_WORKSPACE_PROFILE_ERROR.format(errors="; ".join(errors)))
    security_data = _security_data(profile)
    async with atomic_write() as db:
        jid_cursor = await db.execute(
            "SELECT folder FROM registered_groups WHERE jid = ?",
            (profile.jid,),
        )
        jid_row = await jid_cursor.fetchone()
        if jid_row is not None and jid_row["folder"] != profile.folder:
            raise ValueError(
                f"Chat JID {profile.jid!r} is already owned by workspace {jid_row['folder']!r}"
            )
        old_cursor = await db.execute(
            "SELECT jid FROM registered_groups WHERE folder = ?",
            (profile.folder,),
        )
        old_row = await old_cursor.fetchone()
        old_jid = str(old_row["jid"]) if old_row is not None else None
        if old_jid is not None and old_jid != profile.jid:
            await db.execute("DELETE FROM registered_groups WHERE jid = ?", (old_jid,))
        await _upsert_workspace_profile(db, profile, security_data)
    return old_jid


def _security_data(profile: WorkspaceProfile) -> dict[str, object]:
    return {
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
        "cop_active": profile.security.cop_active,
    }


async def _upsert_workspace_profile(
    db: Connection,
    profile: WorkspaceProfile,
    security_data: dict[str, object],
) -> None:
    await db.execute(
        """
        INSERT INTO registered_groups (
            jid, name, folder, trigger_pattern, added_at,
            container_config, security_profile, is_admin
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(jid) DO UPDATE SET
            name = excluded.name,
            trigger_pattern = excluded.trigger_pattern,
            added_at = excluded.added_at,
            container_config = excluded.container_config,
            security_profile = excluded.security_profile,
            is_admin = excluded.is_admin
        """,
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
