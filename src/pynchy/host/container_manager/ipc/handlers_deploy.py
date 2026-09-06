"""IPC handlers for deployment."""

from __future__ import annotations

from typing import Any

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.logger import logger


async def _handle_deploy(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    """Handle a deploy request from the admin group agent.

    The agent is responsible for git add/commit before calling deploy. This
    handler validates context and starts the Temporal deploy workflow; rebuild,
    continuation writing, and restart signaling happen inside the activity.
    """
    if not is_admin:
        logger.warning(
            "Unauthorized deploy attempt",
            source_group=source_group,
        )
        return

    chat_jid = data.get("chatJid")
    await deps.request_deploy(
        chat_jid=chat_jid if isinstance(chat_jid, str) and chat_jid else None,
        commit_sha=str(data.get("headSha", "")),
        rebuild=bool(data.get("rebuildContainer")),
        resume_prompt=str(data.get("resumePrompt", "Deploy complete. Verifying service health.")),
    )


register("deploy", _handle_deploy)
