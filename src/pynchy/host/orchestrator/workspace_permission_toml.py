"""TOML mutations for workspace capability permissions."""

from typing import Any, cast

import tomlkit


def semantic_policy_table(workspace: object, workspace_name: str) -> object:
    """Find the scoped or threaded workspace that owns a semantic policy."""
    workspace_table = cast("Any", workspace)
    for collection_name in ("scopes", "threads"):
        for candidate in workspace_table.get(collection_name, []):
            if candidate.get("workspace") == workspace_name:
                return candidate
    raise ValueError(f"Workspace '{workspace_name}' has no persistent policy owner")


def grant_capability(target: object, capability_id: str) -> None:
    """Add one exact allow and remove matching exact restrictions."""
    target_table = cast("Any", target)
    permissions = target_table.get("permissions")
    if permissions is None:
        permissions = tomlkit.inline_table()
        target_table["permissions"] = permissions
    for bucket in ("ask", "deny"):
        permissions[bucket] = [
            value for value in permissions.get(bucket, []) if value != capability_id
        ]
    permissions["allow"] = list(
        dict.fromkeys([*[str(value) for value in permissions.get("allow", [])], capability_id])
    )
