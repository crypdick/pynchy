"""Admin-approved repository updates.

Git polling can discover a newer revision without changing the running
checkout. This module renders that pending update through the existing
channel ``ask_user`` surface and performs the fetch/deploy only after the
configured admin accepts it.
"""

from __future__ import annotations

import asyncio
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves these annotations at runtime.
    Awaitable,
    Callable,
)
from typing import Any, Protocol, cast

from pynchy.config import get_settings
from pynchy.host.git_ops.sync_poll import (
    get_deploy_config_hash,
    get_local_head_sha,
    host_update_main,
    needs_container_rebuild,
    needs_deploy,
)
from pynchy.host.orchestrator.adapters import resolve_admin_notification_jid
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.host.orchestrator.temporal.scheduler import start_deploy_workflow
from pynchy.logger import logger
from pynchy.state import advance_deployment_baseline, get_deployment_state
from pynchy.types import Channel, DeployClaimStatus, DeployRevision, WorkspaceProfile

_REQUEST_PREFIX = "host-update:"
_APPROVE_LABEL = "Fetch and upgrade"


class UpdateOfferDeps(Protocol):
    """Capabilities required to handle an accepted update offer."""

    workspaces: dict[str, WorkspaceProfile]

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...


def request_id_for_update(commit_sha: str) -> str:
    """Build a deterministic channel-action ID for one offered revision."""
    return f"{_REQUEST_PREFIX}{commit_sha}"


def update_offer_questions(commit_sha: str) -> list[dict[str, object]]:
    """Return the one-choice prompt rendered by interactive channels."""
    return [
        {
            "header": "Update available",
            "question": (
                f"Pynchy revision {commit_sha[:8]} is available. "
                "Fetch and upgrade this deployment now?"
            ),
            "options": [
                {
                    "label": _APPROVE_LABEL,
                    "description": "Fetch the latest revision and deploy it.",
                }
            ],
        }
    ]


def _interactive_sender(channel: Channel) -> Callable[..., Awaitable[str | None]] | None:
    if not getattr(channel, "supports_direct_ask_user_callbacks", False):
        return None
    sender = getattr(channel, "send_ask_user", None)
    return cast("Callable[..., Awaitable[str | None]] | None", sender) if callable(sender) else None


async def send_update_offer(
    *,
    channels: list[Channel],
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
    chat_jid: str,
    commit_sha: str,
) -> bool:
    """Notify the admin, preferring a channel-native update button."""
    channel = next((candidate for candidate in channels if candidate.owns_jid(chat_jid)), None)
    sender = _interactive_sender(channel) if channel is not None else None
    if sender is not None:
        try:
            message_id = await sender(
                chat_jid,
                request_id_for_update(commit_sha),
                update_offer_questions(commit_sha),
            )
        # allow: exception-handling - a channel widget failure must fall back to host text.
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not send interactive update offer", error=str(exc))
        else:
            if message_id:
                return True

    try:
        await broadcast_host_message(
            chat_jid,
            f"Pynchy update {commit_sha[:8]} is available. "
            "Use the local control-plane `POST /deploy` endpoint to fetch and upgrade it.",
        )
    # allow: exception-handling - record the failed operational notification without failing sync.
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not send update notification", error=str(exc))
        return False
    return True


def _offered_sha(request_id: str) -> str | None:
    """Parse a valid offered commit SHA from a channel callback ID."""
    sha = request_id.removeprefix(_REQUEST_PREFIX)
    if request_id == sha or len(sha) < 7 or len(sha) > 64:
        return None
    return sha if all(char in "0123456789abcdef" for char in sha) else None


def _approved(answer: dict[str, Any]) -> bool:
    response = answer.get("answer")
    if isinstance(response, list):
        return response == [_APPROVE_LABEL]
    return response == _APPROVE_LABEL


def _answer_targets_admin(answer: dict[str, Any], admin_jid: str) -> bool:
    """Reject a callback that names a channel other than the admin workspace."""
    channel_id = answer.get("channel_id")
    return not channel_id or admin_jid.endswith(f":{channel_id}")


async def handle_update_offer_answer(
    request_id: str,
    answer: dict[str, Any],
    deps: UpdateOfferDeps,
) -> bool:
    """Fetch and deploy a revision after its admin channel action is accepted.

    Returns ``True`` when ``request_id`` belongs to this host-owned flow so
    callers do not forward it to the agent's unrelated ``ask_user`` handler.
    """
    offered_sha = _offered_sha(request_id)
    if offered_sha is None:
        return False

    return await _handle_accepted_update_offer(offered_sha, answer, deps)


async def _handle_accepted_update_offer(
    offered_sha: str,
    answer: dict[str, Any],
    deps: UpdateOfferDeps,
) -> bool:
    """Perform the fetch/deploy work after recognizing a host update action."""
    settings = get_settings()
    admin_jid = resolve_admin_notification_jid(
        deps.workspaces, settings.notifications.admin_workspace
    )
    if not admin_jid:
        return True
    if not _answer_targets_admin(answer, admin_jid):
        logger.warning("Rejected update approval from a non-admin channel", commit_sha=offered_sha)
        return True
    if not _approved(answer):
        return True

    deployment = await get_deployment_state()
    applied = deployment.applied
    previous_sha = (
        applied.commit_sha if applied is not None else get_local_head_sha(settings.project_root)
    )
    previous_config_hash = applied.config_hash if applied is not None else ""

    updated = await asyncio.to_thread(host_update_main, settings.project_root)
    if not updated:
        await deps.broadcast_host_message(
            admin_jid,
            f"Could not fetch update {offered_sha[:8]}; the running deployment was unchanged.",
        )
        return True

    current_sha = get_local_head_sha(settings.project_root)
    config_hash = get_deploy_config_hash()
    restart_required = (
        applied is None
        or needs_deploy(previous_sha, current_sha)
        or config_hash != previous_config_hash
    )
    if not restart_required:
        await advance_deployment_baseline(DeployRevision(current_sha, config_hash))
        await deps.broadcast_host_message(
            admin_jid,
            f"Fetched update {current_sha[:8]}. No service restart was needed.",
        )
        return True

    rebuild = applied is None or needs_container_rebuild(previous_sha, current_sha)
    claim = await start_deploy_workflow(
        DeployRequest(
            chat_jid=admin_jid,
            commit_sha=current_sha,
            config_hash=config_hash,
            previous_sha=previous_sha,
            rebuild=rebuild,
            reason="approved_update",
        )
    )
    if claim.status is DeployClaimStatus.CLAIMED:
        await deps.broadcast_host_message(admin_jid, f"Updating Pynchy to {current_sha[:8]}...")
    else:
        await deps.broadcast_host_message(
            admin_jid,
            f"Update {current_sha[:8]} was not started: {claim.status.value}.",
        )
    return True
