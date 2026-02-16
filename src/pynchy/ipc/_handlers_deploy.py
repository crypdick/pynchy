"""IPC handlers for deployment."""

from __future__ import annotations

import subprocess
from typing import Any

from pynchy.config import get_settings
from pynchy.deploy import finalize_deploy
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import register
from pynchy.logger import logger


async def _handle_deploy(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
    deps: IpcDeps,
) -> None:
    """Handle a deploy request from the god group agent.

    The agent is responsible for git add/commit before calling deploy.
    This handler reads the current HEAD (for rollback), optionally rebuilds
    the container, writes a continuation file, and SIGTERMs the process.
    """
    if not is_god:
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
        groups = deps.registered_groups()
        chat_jid = next(
            (jid for jid, g in groups.items() if g.is_god),
            "",
        )
        if not chat_jid:
            logger.error("Deploy request missing chatJid and no god group registered")
            return
        logger.warning(
            "Deploy request missing chatJid, resolved from god group",
            chat_jid=chat_jid,
        )

    if rebuild_container:
        build_script = get_settings().project_root / "container" / "build.sh"
        if build_script.exists():
            logger.info("Rebuilding container image...")
            result = subprocess.run(
                [str(build_script)],
                cwd=str(get_settings().project_root / "container"),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                await _deploy_error(
                    deps,
                    chat_jid,
                    f"Container rebuild failed: {result.stderr[-500:]}",
                )
                return
        else:
            logger.warning(
                "rebuild_container requested but build.sh not found",
            )

    # Merge the god agent's explicit session with all other active sessions
    active_sessions = deps.get_active_sessions()
    if session_id and chat_jid:
        active_sessions[chat_jid] = session_id

    await finalize_deploy(
        broadcast_host_message=deps.broadcast_host_message,
        chat_jid=chat_jid,
        commit_sha=head_sha,
        previous_sha=head_sha,
        session_id=session_id,
        resume_prompt=resume_prompt,
        active_sessions=active_sessions,
    )


async def _deploy_error(
    deps: IpcDeps,
    chat_jid: str,
    message: str,
) -> None:
    """Send a deploy error message back to the god group."""
    logger.error("Deploy failed", error=message)
    await deps.broadcast_host_message(chat_jid, f"Deploy failed: {message}")


register("deploy", _handle_deploy)
