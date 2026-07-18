"""Shared deploy logic used by both IPC and HTTP deploy paths."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess  # noqa: S404, RUF100 - deploy helper invokes the repo-local build script without a shell.
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves deploy annotations at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from pynchy import state as pynchy_state
from pynchy.config import get_settings
from pynchy.host.git_ops.sync_poll import get_deploy_config_hash
from pynchy.host.git_ops.utils import get_head_sha, run_git
from pynchy.logger import logger
from pynchy.types import DeployChangeKind, DeployRevision
from pynchy.utils import write_json_atomic


@dataclass
class BuildResult:
    """Result of a container image build attempt."""

    success: bool
    skipped: bool = False  # True when build.sh doesn't exist
    stderr: str = ""


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of returning the checkout to its last deployed commit."""

    success: bool
    actual_sha: str = ""
    error: str = ""


def current_deploy_revision() -> DeployRevision:
    """Capture the code and configuration revision effective for a restart."""
    return DeployRevision(
        commit_sha=get_head_sha(),
        config_hash=get_deploy_config_hash(),
    )


def rollback_deploy_checkout(previous_sha: str) -> RollbackResult:
    """Reset a failed pre-restart deploy and verify the resulting checkout SHA.

    The current service has not been restarted on this path, so restoring the
    checkout keeps a failed build or handoff from becoming the next boot's code.
    """
    if not previous_sha:
        return RollbackResult(success=False, error="no previous deploy SHA was recorded")

    try:
        result = run_git("reset", "--hard", previous_sha)
    except (OSError, subprocess.SubprocessError) as exc:
        error = f"git reset could not run: {exc}"
        logger.error("Pre-restart deploy rollback failed", previous_sha=previous_sha, error=error)
        return RollbackResult(success=False, error=error)
    if result.returncode != 0:
        error = result.stderr.strip() or "git reset failed"
        logger.error("Pre-restart deploy rollback failed", previous_sha=previous_sha, error=error)
        return RollbackResult(success=False, error=error)

    actual_sha = get_head_sha()
    if actual_sha == "unknown":
        return RollbackResult(success=False, error="could not verify checkout SHA after git reset")

    logger.info("Pre-restart deploy rollback complete", rollback_sha=actual_sha)
    return RollbackResult(success=True, actual_sha=actual_sha)


def build_container_image(*, timeout: int = 600) -> BuildResult:
    """Run src/pynchy/agent/build.sh to rebuild the container image.

    Returns a BuildResult so callers can decide how to handle success/failure.
    This is the single code path for all container image rebuilds.
    """
    build_script = get_settings().project_root / "src" / "pynchy" / "agent" / "build.sh"
    if not build_script.exists():
        logger.warning("Container rebuild requested but build.sh not found")
        return BuildResult(success=True, skipped=True)

    logger.info("Rebuilding container image...")
    result = subprocess.run(  # noqa: S603, RUF100 - executable is the repo-local build.sh path and no shell is used.
        [str(build_script)],
        cwd=str(get_settings().project_root / "src" / "pynchy" / "agent"),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        logger.error("Container rebuild failed", stderr=result.stderr[-500:])
        return BuildResult(success=False, stderr=result.stderr[-500:])

    logger.info("Container image rebuilt successfully")
    return BuildResult(success=True)


async def finalize_deploy(  # noqa: PLR0913, RUF100 - deploy boundary must carry the full restart contract explicitly.
    *,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
    chat_jid: str,
    commit_sha: str,
    config_hash: str,
    previous_sha: str,
    change_kind: DeployChangeKind,
    resume_prompt: str = "Deploy complete. Verifying service health.",
    sigterm_delay: float = 0,
) -> None:
    """Write continuation, notify all UIs, and SIGTERM self.

    Args:
        broadcast_host_message: async callable(jid, text) to store, send,
            and emit a host message to all UIs.
        chat_jid: JID of the chat to notify.
        commit_sha: The HEAD after deploy.
        config_hash: Hash of restart-sensitive configuration after deploy.
        previous_sha: The HEAD before deploy (for rollback).
        change_kind: User-facing distinction between code and config changes.
        resume_prompt: Message injected into the agent on restart.
        sigterm_delay: Seconds to wait before SIGTERM. Use >0 when an HTTP
            response needs to flush before the process dies.
    """
    # The database rows are the source of truth. This snapshot is diagnostic;
    # startup scans the table again so a turn that begins before SIGTERM but
    # after this write is still recovered.
    in_flight_turns = await pynchy_state.get_in_flight_turns()
    continuation: dict[str, object] = {
        "chat_jid": chat_jid,
        "resume_prompt": resume_prompt,
        "commit_sha": commit_sha,
        "config_hash": config_hash,
        "change_kind": change_kind.value,
        "previous_commit_sha": previous_sha,
        "interrupted_turns": [
            {
                "turn_id": turn.turn_id,
                "chat_jid": turn.chat_jid,
                "work_kind": turn.work_kind.value,
            }
            for turn in in_flight_turns
        ],
    }
    continuation_path = get_settings().data_dir / "deploy_continuation.json"
    write_json_atomic(continuation_path, continuation, indent=2)

    # 2. Notify all UIs
    short_sha = commit_sha[:8] if commit_sha else "unknown"
    if chat_jid:
        await broadcast_host_message(
            chat_jid,
            f"Deploying {short_sha} ({change_kind.value})... restarting now.",
        )

    logger.info(
        "Deploy: restarting service",
        commit_sha=commit_sha,
        config_hash=config_hash,
        change_kind=change_kind.value,
        previous_sha=previous_sha,
    )

    # 3. SIGTERM self
    if sigterm_delay > 0:
        loop = asyncio.get_running_loop()
        loop.call_later(sigterm_delay, os.kill, os.getpid(), signal.SIGTERM)
    else:
        os.kill(os.getpid(), signal.SIGTERM)
