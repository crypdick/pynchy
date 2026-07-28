"""Message-context preparation helpers for agent runs."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves message-context callback annotations at runtime.
    Callable,
)
from pathlib import (  # noqa: TC003, RUF100 - beartype resolves message-context path annotations at runtime.
    Path,
)
from typing import Any

import pynchy.types as types  # noqa: TC001, RUF100 - beartype resolves message-context annotations at runtime.
from pynchy.host.orchestrator.messaging import formatter as message_formatter
from pynchy.logger import logger


def _check_dirty_repo(
    group_name: str, dirty_check_file: Path, repo_is_dirty: Callable[[], bool]
) -> list[str]:
    """Consume the dirty-check marker and return any system notices."""
    notices: list[str] = []
    if not dirty_check_file.exists():
        return notices
    try:
        dirty_check_file.unlink()
        if repo_is_dirty():
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
    data_dir: Path,
    group: types.WorkspaceProfile,
    missed_messages: list[types.NewMessage],
    *,
    is_admin_group: bool,
    repo_is_dirty: Callable[[], bool],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Format SDK messages and gather any reset-time system notices."""
    messages = message_formatter.format_messages_for_sdk(missed_messages)
    dirty_check_file = data_dir / "ipc" / group.folder / "needs_dirty_check.json"
    reset_system_notices = (
        _check_dirty_repo(group.name, dirty_check_file, repo_is_dirty) if is_admin_group else []
    )
    return messages, reset_system_notices
