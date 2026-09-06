"""Temporal deploy workflow activity and payload helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from temporalio import activity

from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
)
from pynchy.deployments import (
    DeployChangeKind,
    DeployRevision,
)
from pynchy.host.orchestrator.api import (
    build_container_image,
    finalize_deploy,
    rollback_deploy_checkout,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.schedules import safe_workflow_fragment
from pynchy.logger import logger
from pynchy.state.api import clear_pending_deployment


@dataclass(frozen=True)
class DeployRequest:
    """Serializable deploy request shared by manual and automatic deploy triggers."""

    chat_jid: str
    commit_sha: str
    config_hash: str
    previous_sha: str
    change_kind: DeployChangeKind | None = None
    resume_prompt: str = "Deploy complete. Verifying service health."
    rebuild: bool = True
    reason: str = "manual"
    force: bool = False

    @property
    def revision(self) -> DeployRevision:
        """Return the effective revision claimed by this request."""
        return DeployRevision(self.commit_sha, self.config_hash)


@runtime_checkable
class DeployFailureDeps(Protocol):
    """The notification capability required while recovering a failed deploy."""

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    @property
    def agent_execution_runtime(self) -> AgentExecutionRuntime: ...


def deploy_request_to_payload(request: DeployRequest) -> dict[str, Any]:
    """Convert a DeployRequest to Temporal's plain payload shape."""
    return {
        "chat_jid": request.chat_jid,
        "commit_sha": request.commit_sha,
        "config_hash": request.config_hash,
        "previous_sha": request.previous_sha,
        "change_kind": request.change_kind.value if request.change_kind else None,
        "resume_prompt": request.resume_prompt,
        "rebuild": request.rebuild,
        "reason": request.reason,
        "force": request.force,
    }


def deploy_request_from_payload(payload: dict[str, Any]) -> DeployRequest:
    """Parse a DeployRequest from Temporal's plain payload shape."""
    raw_change_kind = payload.get("change_kind")
    return DeployRequest(
        chat_jid=str(payload.get("chat_jid", "")),
        commit_sha=str(payload.get("commit_sha", "")),
        config_hash=str(payload.get("config_hash", "")),
        previous_sha=str(payload.get("previous_sha", "")),
        change_kind=(
            DeployChangeKind(str(raw_change_kind)) if raw_change_kind is not None else None
        ),
        resume_prompt=str(
            payload.get("resume_prompt", "Deploy complete. Verifying service health.")
        ),
        rebuild=bool(payload.get("rebuild", True)),
        reason=str(payload.get("reason", "manual")),
        force=bool(payload.get("force")),
    )


def deploy_workflow_id(revision: DeployRevision) -> str:
    """Return the Temporal workflow ID for a deploy request."""
    identity = f"{revision.commit_sha or 'unknown'}-{revision.config_hash or 'unknown'}"
    fragment = safe_workflow_fragment(identity) or "unknown"
    return f"pynchy-deploy-{fragment}"


async def rollback_and_report_failure(
    *,
    request: DeployRequest,
    deps: DeployFailureDeps,
    status_id: str,
    failure_result: str,
    error: str,
) -> str:
    """Restore the checkout and tell the admin what is still running."""
    rollback = await asyncio.to_thread(rollback_deploy_checkout, request.previous_sha)
    await clear_pending_deployment(request.revision)
    if rollback.success:
        result = f"{failure_result}_rolled_back"
        message = (
            f"Auto-deploy {request.commit_sha or 'unknown'} failed: {error}\n"
            f"Rolled back to {rollback.actual_sha}.\n"
            "Server health: healthy (current service was not restarted)."
        )
    else:
        result = f"{failure_result}_rollback_failed"
        message = (
            f"Auto-deploy {request.commit_sha or 'unknown'} failed: {error}\n"
            f"Rollback to {request.previous_sha or 'the previous deploy'} failed: "
            f"{rollback.error}.\n"
            "Server health: healthy (current service was not restarted)."
        )

    if request.chat_jid:
        try:
            await deps.broadcast_host_message(request.chat_jid, message)
        except Exception as notify_exc:  # noqa: BLE001 - reporting must not hide the deploy failure.
            logger.error("Failed to report auto-deploy failure", error=str(notify_exc))

    _record_activity_result(status_id, result, error)
    return result


@activity.defn(name="run_deploy")
async def run_deploy(payload: dict[str, Any]) -> str:
    """Run the host deploy handoff from a Temporal activity."""
    request = deploy_request_from_payload(payload)
    status_id = request.commit_sha or request.previous_sha or request.reason
    deps = cast("DeployFailureDeps", _require_scheduler_deps())

    if request.rebuild:
        try:
            build = await asyncio.to_thread(
                build_container_image, deps.agent_execution_runtime.project_root
            )
        # This error feeds the admin notification and checkout rollback.
        except Exception as exc:  # noqa: BLE001  # allow: exception-handling
            error = f"Container rebuild failed: {type(exc).__name__}: {exc}"
            return await rollback_and_report_failure(
                request=request,
                deps=deps,
                status_id=status_id,
                failure_result="build_failed",
                error=error,
            )
        if not build.success and not build.skipped:
            error = f"Container rebuild failed: {build.stderr}"
            return await rollback_and_report_failure(
                request=request,
                deps=deps,
                status_id=status_id,
                failure_result="build_failed",
                error=error,
            )

    try:
        logger.info(
            "Temporal deploy activity requesting restart",
            commit_sha=request.commit_sha,
            previous_sha=request.previous_sha,
            reason=request.reason,
        )
        await finalize_deploy(
            broadcast_host_message=deps.broadcast_host_message,
            chat_jid=request.chat_jid,
            commit_sha=request.commit_sha,
            config_hash=request.config_hash,
            previous_sha=request.previous_sha,
            change_kind=request.change_kind or DeployChangeKind.RESTART,
            data_dir=deps.agent_execution_runtime.data_dir,
            resume_prompt=request.resume_prompt,
            sigterm_delay=0.25,
        )
    # This error feeds the admin notification and checkout rollback.
    except Exception as exc:  # noqa: BLE001  # allow: exception-handling
        error = f"Restart preparation failed: {type(exc).__name__}: {exc}"
        return await rollback_and_report_failure(
            request=request,
            deps=deps,
            status_id=status_id,
            failure_result="restart_failed",
            error=error,
        )

    _record_activity_result(status_id, "restart_requested")
    return "restart_requested"
