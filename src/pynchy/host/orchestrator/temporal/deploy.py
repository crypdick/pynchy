"""Temporal deploy workflow activity and payload helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from temporalio import activity

from pynchy.host.orchestrator.deploy import build_container_image, finalize_deploy
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.schedules import safe_workflow_fragment
from pynchy.logger import logger


@dataclass(frozen=True)
class DeployRequest:
    """Serializable deploy request shared by manual and automatic deploy triggers."""

    chat_jid: str
    commit_sha: str
    previous_sha: str
    resume_prompt: str = "Deploy complete. Verifying service health."
    rebuild: bool = True
    reason: str = "manual"


def deploy_request_to_payload(request: DeployRequest) -> dict[str, Any]:
    """Convert a DeployRequest to Temporal's plain payload shape."""
    return {
        "chat_jid": request.chat_jid,
        "commit_sha": request.commit_sha,
        "previous_sha": request.previous_sha,
        "resume_prompt": request.resume_prompt,
        "rebuild": request.rebuild,
        "reason": request.reason,
    }


def deploy_request_from_payload(payload: dict[str, Any]) -> DeployRequest:
    """Parse a DeployRequest from Temporal's plain payload shape."""
    return DeployRequest(
        chat_jid=str(payload.get("chat_jid", "")),
        commit_sha=str(payload.get("commit_sha", "")),
        previous_sha=str(payload.get("previous_sha", "")),
        resume_prompt=str(
            payload.get("resume_prompt", "Deploy complete. Verifying service health.")
        ),
        rebuild=bool(payload.get("rebuild", True)),
        reason=str(payload.get("reason", "manual")),
    )


def deploy_workflow_id(commit_sha: str) -> str:
    """Return the Temporal workflow ID for a deploy request."""
    fragment = safe_workflow_fragment(commit_sha or "unknown") or "unknown"
    return f"pynchy-deploy-{fragment}"


@activity.defn(name="run_deploy")
async def run_deploy(payload: dict[str, Any]) -> str:
    """Run the host deploy handoff from a Temporal activity."""
    request = deploy_request_from_payload(payload)
    status_id = request.commit_sha or request.previous_sha or request.reason
    deps = _require_scheduler_deps()

    if request.rebuild:
        build = await asyncio.to_thread(build_container_image)
        if not build.success and not build.skipped:
            message = f"Container rebuild failed: {build.stderr}"
            if request.chat_jid:
                await deps.broadcast_host_message(request.chat_jid, f"Deploy failed: {message}")
            _record_activity_result(status_id, "build_failed", message)
            return "build_failed"

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
        previous_sha=request.previous_sha,
        resume_prompt=request.resume_prompt,
        sigterm_delay=0.25,
    )
    _record_activity_result(status_id, "restart_requested")
    return "restart_requested"
