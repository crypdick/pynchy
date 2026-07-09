"""IPC handlers for deployment."""

from __future__ import annotations

from typing import Any

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # noqa: TC001, RUF100 - beartype resolves deploy handler signatures at runtime.
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.logger import logger


async def start_deploy_workflow(request: DeployRequest) -> None:
    """Start deploy workflow lazily so IPC imports do not import the scheduler."""
    from pynchy.host.orchestrator.temporal.scheduler import (
        start_deploy_workflow as _start_deploy_workflow,
    )

    await _start_deploy_workflow(request)


async def _handle_deploy(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
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

    rebuild_container = data.get("rebuildContainer", False)
    resume_prompt = data.get(
        "resumePrompt",
        "Deploy complete. Verifying service health.",
    )
    head_sha = data.get("headSha", "")
    session_id = data.get("sessionId", "")
    chat_jid = data.get("chatJid", "")

    if not chat_jid:
        groups = deps.workspaces()
        from pynchy.host.orchestrator.adapters import find_admin_jid

        chat_jid = find_admin_jid(groups)
        if not chat_jid:
            logger.error("Deploy request missing chatJid and no admin group registered")
            return
        logger.warning(
            "Deploy request missing chatJid, resolved from admin group",
            chat_jid=chat_jid,
        )

    # Merge the admin agent's explicit session with all other active sessions
    active_sessions = deps.get_active_sessions()
    if session_id and chat_jid:
        active_sessions[chat_jid] = session_id

    await start_deploy_workflow(
        DeployRequest(
            chat_jid=chat_jid,
            commit_sha=head_sha,
            previous_sha=head_sha,
            session_id=session_id,
            resume_prompt=resume_prompt,
            active_sessions=active_sessions,
            rebuild=bool(rebuild_container),
            reason="ipc",
        )
    )


register("deploy", _handle_deploy)
