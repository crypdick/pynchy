"""Mount security — validates additional mounts against an allowlist.

Allowlist location: ~/.config/pynchy/mount-allowlist.toml
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pynchy.host.paths import AGENT_EXTRA_MOUNT_CONTAINER_ROOT
from pynchy.logger import logger
from pynchy.workspace.api import (
    AdditionalMount,
    AllowedRoot,
    MountAllowlist,
)


@dataclass
class _MountAllowlistState:
    cached_allowlist: MountAllowlist | None = None
    allowlist_load_error: str | None = None


_state = _MountAllowlistState()


@dataclass(frozen=True)
class _ParsedAllowlist:
    allowed_roots: list[AllowedRoot]
    blocked_patterns: list[str]
    non_admin_read_only: bool


_ERR_REQUIRED_LIST = "{key} must be an array"
_ERR_REQUIRED_BOOL = "{key} must be a boolean"
_ERR_REQUIRED_STRING = "{field_name} must be a string"
_ERR_ALLOWED_ROOT_TABLE = "allowed_roots[{index}] must be a table"


def reset_mount_allowlist_cache() -> None:  # noqa: V103
    """Clear cached mount policy before reloading configuration."""
    _state.cached_allowlist = None
    _state.allowlist_load_error = None


def _required_list(table: Mapping[str, object], key: str) -> list[object]:
    value = table.get(key)
    if not isinstance(value, list):
        raise TypeError(_ERR_REQUIRED_LIST.format(key=key))
    return value


def _required_bool(table: Mapping[str, object], key: str) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        raise TypeError(_ERR_REQUIRED_BOOL.format(key=key))
    return value


def _string_value(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(_ERR_REQUIRED_STRING.format(field_name=field_name))
    return value


def _optional_string_value(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _string_value(value, field_name=field_name)


def _optional_bool_value(value: object, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TypeError(_ERR_REQUIRED_BOOL.format(key=field_name))
    return value


def _parse_allowed_root(raw_root: object, *, index: int) -> AllowedRoot:
    if not isinstance(raw_root, Mapping):
        raise TypeError(_ERR_ALLOWED_ROOT_TABLE.format(index=index))

    return AllowedRoot(
        path=_string_value(raw_root.get("path"), field_name=f"allowed_roots[{index}].path"),
        allow_read_write=_optional_bool_value(
            raw_root.get("allow_read_write"),
            field_name=f"allowed_roots[{index}].allow_read_write",
            default=False,
        ),
        description=_optional_string_value(
            raw_root.get("description"),
            field_name=f"allowed_roots[{index}].description",
        ),
    )


def _parse_allowlist_table(raw_data: Mapping[str, object]) -> _ParsedAllowlist:
    allowed_roots = [
        _parse_allowed_root(raw_root, index=index)
        for index, raw_root in enumerate(_required_list(raw_data, "allowed_roots"))
    ]

    blocked_patterns = [
        _string_value(pattern, field_name=f"blocked_patterns[{index}]")
        for index, pattern in enumerate(_required_list(raw_data, "blocked_patterns"))
    ]

    return _ParsedAllowlist(
        allowed_roots=allowed_roots,
        blocked_patterns=blocked_patterns,
        non_admin_read_only=_required_bool(raw_data, "non_admin_read_only"),
    )


def _parsed_allowlist(
    raw_data: Mapping[str, object],
    *,
    default_blocked_patterns: list[str],
) -> MountAllowlist:
    raw_allowlist = _parse_allowlist_table(raw_data)
    merged_blocked = list(dict.fromkeys(default_blocked_patterns + raw_allowlist.blocked_patterns))
    return MountAllowlist(
        allowed_roots=raw_allowlist.allowed_roots,
        blocked_patterns=merged_blocked,
        non_admin_read_only=raw_allowlist.non_admin_read_only,
    )


def load_mount_allowlist(
    allowlist_path: Path,
    default_blocked_patterns: tuple[str, ...],
) -> MountAllowlist | None:
    """Load the mount allowlist from the external config location.

    Returns None if the file doesn't exist or is invalid.
    Result is cached in memory for the lifetime of the process.
    """
    if _state.cached_allowlist is not None:
        return _state.cached_allowlist

    if _state.allowlist_load_error is not None:
        return None

    if not allowlist_path.exists():
        _state.allowlist_load_error = f"Mount allowlist not found at {allowlist_path}"
        logger.warning(
            "Mount allowlist not found - additional mounts will be BLOCKED. "
            "Create the file to enable additional mounts.",
            path=str(allowlist_path),
        )
        return None

    try:
        content = allowlist_path.read_text(encoding="utf-8")
        # NOTE: Update docs/architecture/security.md § 2 (Mount Security) if
        # the allowlist format or protection semantics change here.
        allowlist = _parsed_allowlist(
            cast("Mapping[str, object]", tomllib.loads(content)),
            default_blocked_patterns=list(default_blocked_patterns),
        )
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        _state.allowlist_load_error = str(exc)
        logger.error(
            "Failed to load mount allowlist - additional mounts will be BLOCKED",
            path=str(allowlist_path),
            error=_state.allowlist_load_error,
        )
        return None
    else:
        _state.cached_allowlist = allowlist
        _state.allowlist_load_error = None
        logger.info(
            "Mount allowlist loaded successfully",
            path=str(allowlist_path),
            allowed_roots=len(allowlist.allowed_roots),
            blocked_patterns=len(allowlist.blocked_patterns),
        )
        return _state.cached_allowlist


def _expand_path(p: str) -> str:
    """Expand ~ to home directory and resolve to absolute path."""
    return str(Path(p).expanduser().resolve())


def _matches_blocked_pattern(real_path: str, blocked_patterns: list[str]) -> str | None:
    """Check if a path matches any blocked pattern."""
    path_parts = Path(real_path).parts

    for pattern in blocked_patterns:
        for part in path_parts:
            if part == pattern or pattern in part:
                return pattern
        if pattern in real_path:
            return pattern

    return None


def _find_allowed_root(real_path: str, allowed_roots: list[AllowedRoot]) -> AllowedRoot | None:
    """Check if a real path is under an allowed root."""
    real_path_path = Path(real_path).resolve()
    for root in allowed_roots:
        expanded_root = _expand_path(root.path)
        try:
            real_root = Path(expanded_root).resolve()
        except OSError:
            continue

        if not real_root.exists():
            continue

        if real_path_path.is_relative_to(real_root):
            return root

    return None


def _is_valid_container_path(container_path: str) -> bool:
    """Validate the relative suffix below the agent's extra-mount root."""
    if ".." in container_path:
        return False
    if container_path.startswith("/"):
        return False
    return bool(container_path and container_path.strip())


@dataclass
class MountValidationResult:
    allowed: bool
    reason: str
    real_host_path: str | None = None
    resolved_container_path: str | None = None
    effective_readonly: bool | None = None


def _resolved_container_path(mount: AdditionalMount) -> str:
    return mount.container_path or Path(mount.host_path).name


def _existing_real_host_path(host_path: str) -> str | None:
    expanded_path = _expand_path(host_path)
    if not Path(expanded_path).exists():
        return None
    return str(Path(expanded_path).resolve())


def _missing_host_path_reason(host_path: str) -> str:
    expanded_path = _expand_path(host_path)
    return f'Host path does not exist: "{host_path}" (expanded: "{expanded_path}")'


def _allowed_root_reason(allowed_root: AllowedRoot) -> str:
    desc = f" ({allowed_root.description})" if allowed_root.description else ""
    return f'Allowed under root "{allowed_root.path}"{desc}'


def _effective_readonly(
    *,
    mount: AdditionalMount,
    is_admin: bool,
    allowlist: MountAllowlist,
    allowed_root: AllowedRoot,
) -> bool:
    if mount.readonly is not False:
        return True

    if not is_admin and allowlist.non_admin_read_only:
        logger.info(
            "Mount forced to read-only for non-admin group",
            mount=mount.host_path,
        )
        return True

    if not allowed_root.allow_read_write:
        logger.info(
            "Mount forced to read-only - root does not allow read-write",
            mount=mount.host_path,
            root=allowed_root.path,
        )
        return True

    return False


def validate_mount(
    mount: AdditionalMount,
    *,
    is_admin: bool,
    allowlist_path: Path,
    default_blocked_patterns: tuple[str, ...],
) -> MountValidationResult:
    """Validate a single additional mount against the allowlist."""
    allowlist = load_mount_allowlist(allowlist_path, default_blocked_patterns)

    if allowlist is None:
        return MountValidationResult(
            allowed=False,
            reason=f"No mount allowlist configured at {allowlist_path}",
        )

    container_path = _resolved_container_path(mount)
    if not _is_valid_container_path(container_path):
        return MountValidationResult(
            allowed=False,
            reason=(
                f'Invalid container path: "{container_path}"'
                ' - must be relative, non-empty, and not contain ".."'
            ),
        )

    real_path = _existing_real_host_path(mount.host_path)
    if real_path is None:
        return MountValidationResult(
            allowed=False,
            reason=_missing_host_path_reason(mount.host_path),
        )

    blocked_match = _matches_blocked_pattern(real_path, allowlist.blocked_patterns)
    if blocked_match is not None:
        return MountValidationResult(
            allowed=False,
            reason=f'Path matches blocked pattern "{blocked_match}": "{real_path}"',
        )

    allowed_root = _find_allowed_root(real_path, allowlist.allowed_roots)
    if allowed_root is None:
        roots_str = ", ".join(_expand_path(r.path) for r in allowlist.allowed_roots)
        return MountValidationResult(
            allowed=False,
            reason=f'Path "{real_path}" is not under any allowed root. Allowed roots: {roots_str}',
        )

    return MountValidationResult(
        allowed=True,
        reason=_allowed_root_reason(allowed_root),
        real_host_path=real_path,
        resolved_container_path=container_path,
        effective_readonly=_effective_readonly(
            mount=mount,
            is_admin=is_admin,
            allowlist=allowlist,
            allowed_root=allowed_root,
        ),
    )


def validate_additional_mounts(
    mounts: list[AdditionalMount],
    group_name: str,
    *,
    is_admin: bool,
    allowlist_path: Path,
    default_blocked_patterns: tuple[str, ...],
) -> list[dict[str, str | bool]]:
    """Validate all additional mounts for a group.

    Returns list of validated mounts (only those that passed).
    """
    validated: list[dict[str, str | bool]] = []

    for mount in mounts:
        result = validate_mount(
            mount,
            is_admin=is_admin,
            allowlist_path=allowlist_path,
            default_blocked_patterns=default_blocked_patterns,
        )

        if result.allowed:
            real_host_path = cast("str", result.real_host_path)
            effective_readonly = cast("bool", result.effective_readonly)
            validated.append(
                {
                    "hostPath": real_host_path,
                    "containerPath": (
                        f"{AGENT_EXTRA_MOUNT_CONTAINER_ROOT}/{result.resolved_container_path}"
                    ),
                    "readonly": effective_readonly,
                }
            )
            logger.debug(
                "Mount validated successfully",
                group=group_name,
                host_path=real_host_path,
                container_path=result.resolved_container_path,
                readonly=effective_readonly,
                reason=result.reason,
            )
        else:
            logger.warning(
                "Additional mount REJECTED",
                group=group_name,
                requested_path=mount.host_path,
                container_path=mount.container_path,
                reason=result.reason,
            )

    return validated


def generate_allowlist_template() -> str:  # noqa: V103
    """Generate a template allowlist file in TOML format for users to customize."""
    return """\
non_admin_read_only = true
blocked_patterns = ["password", "secret", "token"]

[[allowed_roots]]
path = "~/projects"
allow_read_write = true
description = "Development projects"

[[allowed_roots]]
path = "~/repos"
allow_read_write = true
description = "Git repositories"

[[allowed_roots]]
path = "~/Documents/work"
allow_read_write = false
description = "Work documents (read-only)"
"""
