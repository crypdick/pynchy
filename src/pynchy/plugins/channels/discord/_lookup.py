"""Shared Discord lookup/name helpers."""

from __future__ import annotations

import re
from typing import Any

_DISCORD_CHANNEL_NAME_EMPTY = "Discord channel name cannot be empty"


def same_name(left: str | None, right: str | None) -> bool:
    return bool(left and right and left.casefold() == right.casefold())


def discord_user_names(user: Any) -> set[str]:
    return {
        value
        for value in (
            getattr(user, "display_name", None),
            getattr(user, "global_name", None),
            getattr(user, "name", None),
            str(user),
        )
        if isinstance(value, str) and value.strip()
    }


def normalize_discord_channel_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    if not normalized:
        raise ValueError(_DISCORD_CHANNEL_NAME_EMPTY)
    return normalized[:100]
