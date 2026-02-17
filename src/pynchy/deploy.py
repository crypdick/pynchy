"""Shared deploy logic used by both IPC and HTTP deploy paths."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pynchy.config import get_settings
from pynchy.logger import logger


@dataclass
class BuildResult:
    """Result of a container image build attempt."""

    success: bool
    skipped: bool = False  # True when build.sh doesn't exist
    stderr: str = ""


def build_container_image(*, timeout: int = 600) -> BuildResult:
    """Run container/build.sh to rebuild the container image.

    Returns a BuildResult so callers can decide how to handle success/failure.
    This is the single code path for all container image rebuilds.
    """
    build_script = get_settings().project_root / "container" / "build.sh"
    if not build_script.exists():
        logger.warning("Container rebuild requested but build.sh not found")
        return BuildResult(success=True, skipped=True)

    logger.info("Rebuilding container image...")
    result = subprocess.run(
        [str(build_script)],
        cwd=str(get_settings().project_root / "container"),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.error("Container rebuild failed", stderr=result.stderr[-500:])
        return BuildResult(success=False, stderr=result.stderr[-500:])

    logger.info("Container image rebuilt successfully")
    return BuildResult(success=True)


async def finalize_deploy(
    *,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
    chat_jid: str,
    commit_sha: str,
    previous_sha: str,
    session_id: str = "",
    resume_prompt: str = "Deploy complete. Verifying service health.",
    sigterm_delay: float = 0,
    active_sessions: dict[str, str] | None = None,
) -> None:
    """Write continuation, notify all UIs, and SIGTERM self.

    Args:
        broadcast_host_message: async callable(jid, text) to store, send,
            and emit a host message to all UIs.
        chat_jid: JID of the chat to notify.
        commit_sha: The new HEAD after deploy.
        previous_sha: The HEAD before deploy (for rollback).
        session_id: Optional session ID to preserve across restart.
        resume_prompt: Message injected into the agent on restart.
        sigterm_delay: Seconds to wait before SIGTERM. Use >0 when an HTTP
            response needs to flush before the process dies.
        active_sessions: Optional mapping of chat_jid → session_id for all
            active groups. Merged with the single session_id/chat_jid pair.
    """
    # 1. Build merged active_sessions dict
    merged_sessions: dict[str, str] = dict(active_sessions) if active_sessions else {}
    if session_id and chat_jid:
        merged_sessions[chat_jid] = session_id

    # 2. Write continuation file
    continuation: dict[str, object] = {
        "chat_jid": chat_jid,
        "session_id": session_id,
        "resume_prompt": resume_prompt,
        "commit_sha": commit_sha,
        "previous_commit_sha": previous_sha,
        "active_sessions": merged_sessions,
    }
    continuation_path = get_settings().data_dir / "deploy_continuation.json"
    continuation_path.parent.mkdir(parents=True, exist_ok=True)
    continuation_path.write_text(json.dumps(continuation, indent=2))

    # 3. Notify all UIs
    short_sha = commit_sha[:8] if commit_sha else "unknown"
    if chat_jid:
        await broadcast_host_message(
            chat_jid,
            f"Deploying {short_sha}... restarting now.",
        )

    logger.info(
        "Deploy: restarting service",
        commit_sha=commit_sha,
        previous_sha=previous_sha,
    )

    # 4. SIGTERM self
    if sigterm_delay > 0:
        loop = asyncio.get_running_loop()
        loop.call_later(sigterm_delay, os.kill, os.getpid(), signal.SIGTERM)
    else:
        os.kill(os.getpid(), signal.SIGTERM)
