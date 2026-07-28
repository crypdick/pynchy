"""Temporal activities for host and external repository sync polling."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
from typing import TYPE_CHECKING, NoReturn, cast

from temporalio import activity
from temporalio.exceptions import ApplicationError

from pynchy.config.api import get_settings
from pynchy.deployments import DeployRevision
from pynchy.host.git_ops.api import (
    HostSyncState,
    check_local_head_drift,
    check_origin_drift,
    find_pynchy_repo_ctx,
    get_deploy_config_hash,
    get_local_head_sha,
    get_repo_context,
    git_env_with_token,
    host_get_origin_main_sha,
    host_notify_worktree_updates,
    host_update_main_result,
    last_notified_sha,
    probe_origin_main_sha,
)
from pynchy.host.orchestrator.api import SessionManager, resolve_admin_notification_jid
from pynchy.host.orchestrator.temporal.deploy import (
    DeployFailureDeps,
    DeployRequest,
    rollback_and_report_failure,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_tracked_activity_result,
    _require_scheduler_deps,
)
from pynchy.logger import logger
from pynchy.state.api import (
    advance_deployment_baseline,
    get_deployment_state,
    get_router_state,
    set_router_state,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001, RUF100 - beartype resolves contract annotations at runtime.
)

if TYPE_CHECKING:
    from pynchy.host.orchestrator.scheduler_deps import SchedulerDependencies

HOST_GIT_SYNC_ID = "git-sync-host"
HOST_STATE_KEY = "temporal_git_sync_host_state"
EXTERNAL_GIT_SYNC_PREFIX = "git-sync-repo:"
_EXTERNAL_STATE_PREFIX = "temporal_git_sync_external_state:"
_RUNTIME_HARNESS_ENV = "PYNCHY_RUNTIME_HARNESS"
_check_local_head_drift = check_local_head_drift
_find_pynchy_repo_ctx = find_pynchy_repo_ctx


def _workspace_map(deps: object) -> dict[str, WorkspaceProfile]:
    workspaces = getattr(deps, "workspaces", {})
    workspaces = workspaces() if callable(workspaces) else workspaces
    return cast("dict[str, WorkspaceProfile]", workspaces)


class _TemporalGitSyncDeps:
    """Adapter that lets existing git-sync helpers start Temporal deploys."""

    def __init__(self, deps: object, *, reason: str) -> None:
        self._deps = cast("SchedulerDependencies", deps)
        self._reason = reason

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        await self._deps.broadcast_host_message(jid, text)

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        await self._deps.broadcast_system_notice(jid, text)

    def has_active_session(self, group_folder: str) -> bool:
        if hasattr(self._deps, "has_active_session"):
            return bool(self._deps.has_active_session(group_folder))
        manager = SessionManager(
            getattr(self._deps, "sessions", {}),
            getattr(self._deps, "session_cleared", set()),
        )
        return manager.has_active_session(group_folder)

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return _workspace_map(self._deps)

    def sync_personalization(self, project_root: Path) -> str:
        return self._deps.sync_personalization(project_root)

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
        from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415, RUF100 - avoids scheduler <-> git_sync import cycle.
            start_deploy_workflow,
        )

        workspaces = self.workspaces()
        request = DeployRequest(
            chat_jid=resolve_admin_notification_jid(
                workspaces, get_settings().notifications.admin_workspace
            ),
            commit_sha=get_local_head_sha(get_settings().project_root),
            config_hash=get_deploy_config_hash(),
            previous_sha=previous_sha,
            rebuild=rebuild,
            reason=self._reason,
        )
        try:
            await start_deploy_workflow(request)
        # allow: exception-handling - scheduler failure must restore a changed checkout.
        except Exception as exc:
            error = f"Could not start deploy workflow: {type(exc).__name__}: {exc}"
            await rollback_and_report_failure(
                request=request,
                deps=cast("DeployFailureDeps", self._deps),
                status_id=request.commit_sha or request.previous_sha or request.reason,
                failure_result="workflow_start_failed",
                error=error,
            )
            raise

    async def offer_update(self, commit_sha: str) -> bool:
        """Ask the configured admin to approve a pending repository update."""
        chat_jid = resolve_admin_notification_jid(
            self.workspaces(), get_settings().notifications.admin_workspace
        )
        if not chat_jid:
            logger.error(
                "Cannot offer pending update without an admin workspace", commit_sha=commit_sha
            )
            return False
        offer_update = getattr(self._deps, "offer_update", None)
        if callable(offer_update):
            return bool(await offer_update(chat_jid, commit_sha))
        try:
            await self._deps.broadcast_host_message(
                chat_jid,
                f"Pynchy update {commit_sha[:8]} is available. "
                "Use the local control-plane `POST /deploy` endpoint to fetch and upgrade it.",
            )
        # allow: exception-handling - retry a failed text notification on the next poll.
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not send update fallback notification", error=str(exc))
            return False
        return True


async def _load_host_state() -> HostSyncState:
    settings = get_settings()
    raw = await get_router_state(HOST_STATE_KEY)
    state: HostSyncState | None = None
    if raw:
        try:
            payload = json.loads(raw)
            state = HostSyncState(
                last_origin_sha=payload.get("last_origin_sha"),
                deployed_sha=str(payload.get("deployed_sha", "")),
                config_hash=str(payload.get("config_hash", "")),
                local_head=payload.get("local_head"),
                offered_sha=str(payload.get("offered_sha", "")),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Corrupt Temporal host git-sync state, reinitializing")

    if state is None:
        state = HostSyncState(
            last_origin_sha=await asyncio.to_thread(
                host_get_origin_main_sha, settings.project_root
            ),
            deployed_sha=await asyncio.to_thread(get_local_head_sha, settings.project_root),
            config_hash=await asyncio.to_thread(get_deploy_config_hash),
        )

    deployment = await get_deployment_state()
    if deployment.applied is not None:
        state.deployed_sha = deployment.applied.commit_sha
        state.config_hash = deployment.applied.config_hash
    return state


async def _save_host_state(state: HostSyncState) -> None:
    await set_router_state(HOST_STATE_KEY, json.dumps(asdict(state)))


async def _config_drift_started_deploy(state: HostSyncState, deps: _TemporalGitSyncDeps) -> bool:
    current_config_hash = await asyncio.to_thread(get_deploy_config_hash)
    if current_config_hash == state.config_hash:
        return False
    logger.info("Config files changed, starting Temporal deploy")
    await deps.trigger_deploy(state.deployed_sha, rebuild=False)
    return True


@activity.defn(name="run_host_git_sync")
async def run_host_git_sync() -> str:
    """Run one host-repo git sync poll through Temporal."""
    if os.environ.get(_RUNTIME_HARNESS_ENV) == "1":
        # The hermetic runtime owns its process lifecycle directly. Its
        # generated config is expected to change across feature work, so the
        # production deploy handoff would SIGTERM an unsupervised test process.
        _record_tracked_activity_result(HOST_GIT_SYNC_ID, "skipped")
        return "skipped"

    deps = _TemporalGitSyncDeps(_require_scheduler_deps(), reason="host_git_sync")
    settings = get_settings()
    state = await _load_host_state()
    repo_ctx = _find_pynchy_repo_ctx(tuple(settings.repos.overrides), settings.project_root)
    result = "idle"

    try:
        personalization_result = await asyncio.to_thread(
            deps.sync_personalization,
            settings.project_root,
        )
        if personalization_result == "pushed":
            result = "personalization_pushed"
        if (
            await _config_drift_started_deploy(state, deps)
            or await _check_local_head_drift(
                settings.project_root,
                state,
                repo_ctx,
                deps,
                auto_deploy=settings.scheduler.auto_deploy,
            )
            or await check_origin_drift(
                settings.project_root,
                state,
                repo_ctx,
                deps,
                auto_deploy=settings.scheduler.auto_deploy,
            )
        ):
            result = "deploy_started"
    finally:
        if result != "deploy_started" and state.deployed_sha and state.config_hash:
            await advance_deployment_baseline(DeployRevision(state.deployed_sha, state.config_hash))
        await _save_host_state(state)

    _record_tracked_activity_result(HOST_GIT_SYNC_ID, result)
    return result


async def _load_external_origin(repo_slug: str) -> str | None:
    return await get_router_state(f"{_EXTERNAL_STATE_PREFIX}{repo_slug}")


async def _save_external_origin(repo_slug: str, origin_sha: str) -> None:
    await set_router_state(f"{_EXTERNAL_STATE_PREFIX}{repo_slug}", origin_sha)


def _raise_external_git_sync_failure(
    repo_slug: str,
    *,
    result: str,
    error_type: str,
    diagnostic: str,
) -> NoReturn:
    task_id = f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}"
    error = f"External git sync {result.replace('_', ' ')} for {repo_slug}: {diagnostic}"
    logger.warning(
        "External git sync failed",
        slug=repo_slug,
        result=result,
        error=diagnostic,
    )
    _record_tracked_activity_result(task_id, result, error)
    raise ApplicationError(error, type=error_type, non_retryable=True)


@activity.defn(name="run_external_git_sync")
async def run_external_git_sync(repo_slug: str) -> str:
    """Run one external-repo sync poll through Temporal."""
    repo_ctx = get_repo_context(repo_slug)
    if repo_ctx is None:
        _record_tracked_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "skipped")
        return "skipped"

    deps = _TemporalGitSyncDeps(_require_scheduler_deps(), reason="external_git_sync")
    env = git_env_with_token(repo_slug)
    probe = await asyncio.to_thread(probe_origin_main_sha, repo_ctx.root, env)
    if probe.sha is None:
        _raise_external_git_sync_failure(
            repo_slug,
            result="unavailable",
            error_type="ExternalGitSyncUnavailable",
            diagnostic=probe.error or "origin/main did not return a revision",
        )
    current_origin = probe.sha

    last_origin = await _load_external_origin(repo_slug)
    if not last_origin:
        await _save_external_origin(repo_slug, current_origin)
        _record_tracked_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "initialized")
        return "initialized"
    if current_origin == last_origin:
        _record_tracked_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "idle")
        return "idle"

    update = await asyncio.to_thread(host_update_main_result, repo_ctx.root, env)
    if not update.succeeded:
        _raise_external_git_sync_failure(
            repo_slug,
            result="sync_failed",
            error_type="ExternalGitSyncFailed",
            diagnostic=update.error or "repository update failed without a diagnostic",
        )

    await _save_external_origin(repo_slug, current_origin)
    new_head = await asyncio.to_thread(get_local_head_sha, repo_ctx.root)
    if last_notified_sha.get(str(repo_ctx.root), "") != new_head:
        await host_notify_worktree_updates(None, deps, repo_ctx)

    _record_tracked_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "synced")
    return "synced"
