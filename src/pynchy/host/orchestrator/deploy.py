"""Shared deploy logic used by both IPC and HTTP deploy paths."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess  # noqa: S404 - deploy helper invokes the repo-local build script without a shell.
from collections.abc import (  # noqa: TC003 - beartype resolves deploy annotations at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from pathlib import (
    Path,  # noqa: TC003 - beartype resolves deploy path annotations at runtime.
)
from typing import Any, cast

from pynchy.atomic_json import write_json_atomic
from pynchy.deployments import (
    DeployChangeKind,
    DeployRevision,
)
from pynchy.logger import logger
from pynchy.state import api as pynchy_state

_CONTAINER_BUILD_TIMEOUT_SECONDS = 180
_APPLE_BUILD_LOCK_TIMEOUT_SECONDS = 60
_RELEASE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


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


@dataclass(frozen=True)
class DeployGitRuntime:
    """Git operations selected by the application composition root."""

    get_head_sha: Callable[[], str]
    get_deploy_config_hash: Callable[[], str]
    run_git: Callable[..., object]


_runtime: DeployGitRuntime | None = None


def configure_deploy_git_runtime(runtime: DeployGitRuntime) -> None:
    """Set concrete Git operations before deploy handling starts."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> DeployGitRuntime:
    if _runtime is None:
        raise RuntimeError("Deploy Git runtime has not been configured")
    return _runtime


def current_deploy_revision() -> DeployRevision:
    """Capture the code and configuration revision effective for a restart."""
    runtime = _configured_runtime()
    release_sha = os.environ.get("PYNCHY_RELEASE_SHA", "")
    if release_sha and _RELEASE_SHA_PATTERN.fullmatch(release_sha) is None:
        raise RuntimeError("PYNCHY_RELEASE_SHA must be a full lowercase Git commit SHA")
    return DeployRevision(
        commit_sha=release_sha or runtime.get_head_sha(),
        config_hash=runtime.get_deploy_config_hash(),
    )


def rollback_deploy_checkout(previous_sha: str) -> RollbackResult:
    """Reset a failed pre-restart deploy and verify the resulting checkout SHA.

    The current service has not been restarted on this path, so restoring the
    checkout keeps a failed build or handoff from becoming the next boot's code.
    """
    if not previous_sha:
        return RollbackResult(success=False, error="no previous deploy SHA was recorded")
    runtime = _configured_runtime()
    return rollback_checkout(
        previous_sha,
        get_head_sha=runtime.get_head_sha,
        run_git=runtime.run_git,
    )


def rollback_checkout(
    previous_sha: str,
    *,
    get_head_sha: Callable[[], str],
    run_git: Callable[..., object],
) -> RollbackResult:
    """Reset one deploy checkout without discarding operator-owned changes."""
    if not previous_sha:
        return RollbackResult(success=False, error="no previous deploy SHA was recorded")

    stashed, error = _stash_dirty_work(run_git)
    if error is not None:
        return RollbackResult(success=False, error=error)
    error = _reset_checkout(run_git, previous_sha)
    if error is not None:
        if stashed and (restore_error := _restore_rollback_stash(run_git)):
            error = f"{error}; {restore_error}"
        logger.error("Pre-restart deploy rollback failed", previous_sha=previous_sha, error=error)
        return RollbackResult(success=False, error=error)
    if stashed and (restore_error := _restore_rollback_stash(run_git)):
        return RollbackResult(success=False, error=restore_error)

    actual_sha = get_head_sha()
    if actual_sha == "unknown":
        return RollbackResult(success=False, error="could not verify checkout SHA after git reset")

    logger.info("Pre-restart deploy rollback complete", rollback_sha=actual_sha)
    return RollbackResult(success=True, actual_sha=actual_sha)


def _stash_dirty_work(run_git: Callable[..., object]) -> tuple[bool, str | None]:
    try:
        status = cast("Any", run_git("status", "--porcelain", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git status could not run: {exc}"
    if status.returncode != 0:
        return False, f"git status failed: {status.stderr.strip() or 'unknown error'}"
    if not status.stdout.strip():
        return False, None
    try:
        result = cast("Any", run_git("stash", "push", "--include-untracked"))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git stash could not run: {exc}"
    if result.returncode == 0:
        return "No local changes" not in result.stdout, None
    return False, result.stderr.strip() or "git stash failed"


def _reset_checkout(run_git: Callable[..., object], previous_sha: str) -> str | None:
    try:
        result = cast("Any", run_git("reset", "--hard", previous_sha))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git reset could not run: {exc}"
    return None if result.returncode == 0 else result.stderr.strip() or "git reset failed"


def _restore_rollback_stash(run_git: Callable[..., object]) -> str | None:
    try:
        result = cast("Any", run_git("stash", "pop"))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git stash restore could not run: {exc}"
    if result.returncode == 0:
        return None
    return result.stderr.strip() or "git stash restore failed; local work remains in the stash"


def build_container_image(
    project_root: Path, *, timeout: int = _CONTAINER_BUILD_TIMEOUT_SECONDS
) -> BuildResult:
    """Run src/pynchy/agent/build.sh to rebuild the container image.

    Returns a BuildResult so callers can decide how to handle success/failure.
    This is the single code path for all container image rebuilds.
    """
    build_script = project_root / "src" / "pynchy" / "agent" / "build.sh"
    if not build_script.exists():
        logger.warning("Container rebuild requested but build.sh not found")
        return BuildResult(success=True, skipped=True)

    # Apple builds may wait for another Pynchy build to leave the shared
    # builder, then retain their full 180-second execution budget.
    logger.info("Rebuilding container image...")
    result = subprocess.run(  # noqa: S603 - executable is the repo-local build.sh path and no shell is used.
        [str(build_script)],
        cwd=str(project_root / "src" / "pynchy" / "agent"),
        capture_output=True,
        text=True,
        timeout=timeout + _APPLE_BUILD_LOCK_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        logger.error("Container rebuild failed", stderr=result.stderr[-500:])
        return BuildResult(success=False, stderr=result.stderr[-500:])

    logger.info("Container image rebuilt successfully")
    return BuildResult(success=True)


async def finalize_deploy(  # noqa: PLR0913 - deploy boundary must carry the full restart contract explicitly.
    *,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
    chat_jid: str,
    commit_sha: str,
    config_hash: str,
    previous_sha: str,
    change_kind: DeployChangeKind,
    data_dir: Path,
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
    continuation_path = data_dir / "deploy_continuation.json"
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
