"""Shared deploy logic used by both IPC and HTTP deploy paths."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable

from pynchy.config import DATA_DIR
from pynchy.logger import logger
from pynchy.router import format_system_message


async def finalize_deploy(
    *,
    broadcast_system_message: Callable[[str, str], Awaitable[None]],
    chat_jid: str,
    commit_sha: str,
    previous_sha: str,
    session_id: str = "",
    resume_prompt: str = "Deploy complete. Verifying service health.",
    sigterm_delay: float = 0,
) -> None:
    """Write continuation, notify all UIs, and SIGTERM self.

    Args:
        broadcast_system_message: async callable(jid, text) to store, send,
            and emit a system message to all UIs.
        chat_jid: JID of the chat to notify.
        commit_sha: The new HEAD after deploy.
        previous_sha: The HEAD before deploy (for rollback).
        session_id: Optional session ID to preserve across restart.
        resume_prompt: Message injected into the agent on restart.
        sigterm_delay: Seconds to wait before SIGTERM. Use >0 when an HTTP
            response needs to flush before the process dies.
    """
    # 1. Write continuation file
    continuation = {
        "chat_jid": chat_jid,
        "session_id": session_id,
        "resume_prompt": resume_prompt,
        "commit_sha": commit_sha,
        "previous_commit_sha": previous_sha,
    }
    continuation_path = DATA_DIR / "deploy_continuation.json"
    continuation_path.parent.mkdir(parents=True, exist_ok=True)
    continuation_path.write_text(json.dumps(continuation, indent=2))

    # 2. Notify all UIs
    short_sha = commit_sha[:8] if commit_sha else "unknown"
    if chat_jid:
        await broadcast_system_message(
            chat_jid,
            format_system_message(f"Deploying {short_sha}... restarting now."),
        )

    logger.info(
        "Deploy: restarting service",
        commit_sha=commit_sha,
        previous_sha=previous_sha,
    )

    # 3. SIGTERM self
    if sigterm_delay > 0:
        loop = asyncio.get_running_loop()
        loop.call_later(sigterm_delay, os.kill, os.getpid(), signal.SIGTERM)
    else:
        os.kill(os.getpid(), signal.SIGTERM)
