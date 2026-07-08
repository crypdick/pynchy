"""Message-context preparation helpers for agent runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pynchy.types as types
from pynchy.config.settings import Settings
from pynchy.host.git_ops.utils import is_repo_dirty
from pynchy.logger import logger


def _check_dirty_repo(group_name: str, dirty_check_file: Path) -> list[str]:
    """Consume the dirty-check marker and return any system notices."""
    notices: list[str] = []
    if not dirty_check_file.exists():
        return notices
    try:
        dirty_check_file.unlink()
        if is_repo_dirty():
            notices.append(
                "WARNING: Uncommitted changes detected in the repository. "
                "Please review and commit these changes so that you may work "
                "with a clean slate. "
                "Run `git status` and `git diff` to see what has changed."
            )
            logger.info("Added dirty repo warning after reset", group=group_name)
    except OSError as exc:
        logger.error("Error checking for dirty repo after reset", err=str(exc))
        dirty_check_file.unlink(missing_ok=True)
    return notices


def prepare_message_context(
    s: Settings,
    group: types.WorkspaceProfile,
    missed_messages: list[types.NewMessage],
    is_admin_group: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Format SDK messages and gather any reset-time system notices."""
    from pynchy.host.orchestrator.messaging.formatter import format_messages_for_sdk

    messages = format_messages_for_sdk(missed_messages)
    dirty_check_file = s.data_dir / "ipc" / group.folder / "needs_dirty_check.json"
    reset_system_notices = _check_dirty_repo(group.name, dirty_check_file) if is_admin_group else []
    return messages, reset_system_notices
