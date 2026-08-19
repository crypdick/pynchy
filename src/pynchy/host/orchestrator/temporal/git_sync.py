"""Temporal activities for host and external repository sync polling."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves temporal Git runtime annotations.
)
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 - beartype resolves this runtime annotation.
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast

from temporalio import activity
from temporalio.exceptions import ApplicationError

from pynchy.deployments import DeployRevision
from pynchy.host.orchestrator.api import SessionManager, resolve_admin_notification_jid
from pynchy.host.orchestrator.config_refresh import ConfigRefreshStatus
from pynchy.host.orchestrator.scheduler_deps import HostSyncState
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
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

if TYPE_CHECKING:
    from pynchy.host.orchestrator.scheduler_deps import SchedulerDependencies

HOST_GIT_SYNC_ID = "git-sync-host"
HOST_STATE_KEY = "temporal_git_sync_host_state"
EXTERNAL_GIT_SYNC_PREFIX = "git-sync-repo:"
_EXTERNAL_STATE_PREFIX = "temporal_git_sync_external_state:"
_RUNTIME_HARNESS_ENV = "PYNCHY_RUNTIME_HARNESS"
_DISK_CAPACITY_STATE_KEY = "host_disk_capacity_state"
_DISK_LOW_BYTES = 5 * 1024**3
_WORKTREE_VENV_GC_STATE_KEY = "worktree_venv_gc_last_run"
_WORKTREE_VENV_GC_INTERVAL = timedelta(days=1)


def _unconfigured_runtime(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("Temporal Git sync has not been composed")


@dataclass(frozen=True)
class TemporalGitSyncRuntime:
    get_settings: Callable[[], Any]
    check_local_head_drift: Callable[..., Any]
    check_origin_drift: Callable[..., Any]
    find_pynchy_repo_ctx: Callable[..., Any]
    get_deploy_config_hash: Callable[[], str]
    get_local_head_sha: Callable[..., str]
    get_repo_context: Callable[..., Any]
    git_env_with_token: Callable[..., Any]
    host_get_origin_main_sha: Callable[..., Any]
    host_notify_worktree_updates: Callable[..., Any]
    host_update_main_result: Callable[..., Any]
    last_notified_sha: dict[str, str]
    needs_deploy: Callable[[str, str], bool]
    probe_origin_main_sha: Callable[..., Any]
    prune_stale_worktree_venvs: Callable[..., list[Path]]
    refresh_host_config: Callable[[str], Any]


_runtime = TemporalGitSyncRuntime(
    get_settings=_unconfigured_runtime,
    check_local_head_drift=_unconfigured_runtime,
    check_origin_drift=_unconfigured_runtime,
    find_pynchy_repo_ctx=_unconfigured_runtime,
    get_deploy_config_hash=_unconfigured_runtime,
    get_local_head_sha=_unconfigured_runtime,
    get_repo_context=_unconfigured_runtime,
    git_env_with_token=_unconfigured_runtime,
    host_get_origin_main_sha=_unconfigured_runtime,
    host_notify_worktree_updates=_unconfigured_runtime,
    host_update_main_result=_unconfigured_runtime,
    last_notified_sha={},
    needs_deploy=_unconfigured_runtime,
    probe_origin_main_sha=_unconfigured_runtime,
    prune_stale_worktree_venvs=_unconfigured_runtime,
    refresh_host_config=_unconfigured_runtime,
)


def configure_temporal_git_sync_runtime(runtime: TemporalGitSyncRuntime) -> None:
    """Bind Temporal Git sync operations at host composition."""
    global _runtime, get_settings, _check_local_head_drift, _find_pynchy_repo_ctx  # noqa: PLW0603 - one host process owns Temporal Git sync operations.
    global get_deploy_config_hash, get_local_head_sha, get_repo_context, git_env_with_token  # noqa: PLW0603 - one host process owns Temporal Git sync operations.
    global host_get_origin_main_sha, host_notify_worktree_updates  # noqa: PLW0603 - one host process owns Temporal Git sync operations.
    global host_update_main_result, check_origin_drift  # noqa: PLW0603 - one host process owns Temporal Git sync operations.
    global probe_origin_main_sha, last_notified_sha, needs_deploy, refresh_host_config  # noqa: PLW0603 - one host process owns Temporal Git sync operations.
    global prune_stale_worktree_venvs  # noqa: PLW0603 - composition owns this source-control port.
    _runtime = runtime
    get_settings = runtime.get_settings
    _check_local_head_drift = runtime.check_local_head_drift
    _find_pynchy_repo_ctx = runtime.find_pynchy_repo_ctx
    get_deploy_config_hash = runtime.get_deploy_config_hash
    get_local_head_sha = runtime.get_local_head_sha
    get_repo_context = runtime.get_repo_context
    git_env_with_token = runtime.git_env_with_token
    host_get_origin_main_sha = runtime.host_get_origin_main_sha
    host_notify_worktree_updates = runtime.host_notify_worktree_updates
    host_update_main_result = runtime.host_update_main_result
    check_origin_drift = runtime.check_origin_drift
    probe_origin_main_sha = runtime.probe_origin_main_sha
    prune_stale_worktree_venvs = runtime.prune_stale_worktree_venvs
    last_notified_sha = runtime.last_notified_sha
    needs_deploy = runtime.needs_deploy
    refresh_host_config = runtime.refresh_host_config


get_settings: Callable[[], Any] = _unconfigured_runtime
_check_local_head_drift: Callable[..., Any] = _unconfigured_runtime
_find_pynchy_repo_ctx: Callable[..., Any] = _unconfigured_runtime
get_deploy_config_hash: Callable[[], str] = _unconfigured_runtime
get_local_head_sha: Callable[..., str] = _unconfigured_runtime
get_repo_context: Callable[..., Any] = _unconfigured_runtime
git_env_with_token: Callable[..., Any] = _unconfigured_runtime
host_get_origin_main_sha: Callable[..., Any] = _unconfigured_runtime
host_notify_worktree_updates: Callable[..., Any] = _unconfigured_runtime
host_update_main_result: Callable[..., Any] = _unconfigured_runtime
check_origin_drift: Callable[..., Any] = _unconfigured_runtime
probe_origin_main_sha: Callable[..., Any] = _unconfigured_runtime
prune_stale_worktree_venvs: Callable[..., list[Path]] = _unconfigured_runtime
last_notified_sha: dict[str, str] = {}
needs_deploy: Callable[[str, str], bool] = _unconfigured_runtime
refresh_host_config: Callable[[str], Any] = _unconfigured_runtime


def _workspace_map(deps: object) -> dict[str, WorkspaceProfile]:
    workspaces = getattr(deps, "workspaces", {})
    workspaces = workspaces() if callable(workspaces) else workspaces
    return cast("dict[str, WorkspaceProfile]", workspaces)


class _HostNotificationDeps(Protocol):
    async def broadcast_host_message(self, jid: str, text: str) -> None: ...


async def _report_disk_capacity(deps: _HostNotificationDeps) -> None:
    """Notify the Discord admin once when host disk capacity becomes critical."""
    settings = get_settings()
    try:
        usage = await asyncio.to_thread(shutil.disk_usage, settings.project_root)
    except OSError as exc:
        logger.warning("Could not inspect host disk capacity", error=str(exc))
        return

    low = usage.free < _DISK_LOW_BYTES or usage.free / usage.total < 0.05
    state = "low" if low else "ok"
    previous = await get_router_state(_DISK_CAPACITY_STATE_KEY)
    if state == previous:
        return

    if low or previous == "low":
        chat_jid = resolve_admin_notification_jid(
            _workspace_map(deps), settings.notifications.admin_workspace
        )
        if not chat_jid:
            return
        free_gib = usage.free / 1024**3
        percent_free = usage.free / usage.total * 100
        message = (
            f"ERROR: Host disk space critically low: {free_gib:.1f} GiB free "
            f"({percent_free:.1f}%). Free space before scheduled work continues."
            if low
            else f"Host disk space recovered: {free_gib:.1f} GiB free ({percent_free:.1f}%)."
        )
        await deps.broadcast_host_message(chat_jid, message)

    await set_router_state(_DISK_CAPACITY_STATE_KEY, state)


class _TemporalGitSyncDeps:
    """Adapter that lets existing git-sync helpers start Temporal deploys."""

    def __init__(self, deps: object, *, reason: str) -> None:
        self._deps = cast("SchedulerDependencies", deps)
        self._reason = reason

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        await self._deps.broadcast_host_message(jid, text)

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        await self._deps.broadcast_system_notice(jid, text)

    async def wake_worktree_conflict(self, jid: str) -> None:
        from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415 - avoids temporal activity/scheduler import cycle.
            start_interactive_message_workflow,
        )

        await start_interactive_message_workflow(jid)

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

    def active_worktree_folders(self) -> set[str]:
        getter = getattr(self._deps, "active_worktree_folders", None)
        return set(getter()) if callable(getter) else set()

    def sync_personalization(self, project_root: Path) -> str:
        return self._deps.sync_personalization(project_root)

    async def trigger_deploy(
        self,
        previous_sha: str,
        *,
        rebuild: bool = True,
        config_hash: str | None = None,
    ) -> None:
        from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415 - avoids scheduler <-> git_sync import cycle.
            start_deploy_workflow,
        )

        workspaces = self.workspaces()
        request = DeployRequest(
            chat_jid=resolve_admin_notification_jid(
                workspaces, get_settings().notifications.admin_workspace
            ),
            commit_sha=get_local_head_sha(get_settings().project_root),
            config_hash=config_hash or get_deploy_config_hash(),
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


async def _prune_stale_venvs(deps: _TemporalGitSyncDeps) -> None:
    """Run managed-worktree venv retention at most once per day."""
    now = datetime.now(UTC)
    last_run = await get_router_state(_WORKTREE_VENV_GC_STATE_KEY)
    if last_run:
        try:
            last_run_at = datetime.fromisoformat(last_run)
            if last_run_at.tzinfo is not None and now - last_run_at < _WORKTREE_VENV_GC_INTERVAL:
                return
        except ValueError:
            logger.warning("Corrupt worktree venv GC state, running cleanup")
    settings = get_settings()
    await asyncio.to_thread(
        prune_stale_worktree_venvs,
        settings.worktrees_dir,
        active_folders=deps.active_worktree_folders(),
    )
    await set_router_state(_WORKTREE_VENV_GC_STATE_KEY, now.isoformat())


def _local_code_awaits_approval(state: HostSyncState, *, auto_deploy: bool) -> bool:
    return bool(
        not auto_deploy
        and state.local_head
        and state.deployed_sha
        and state.local_head != state.deployed_sha
        and needs_deploy(state.deployed_sha, state.local_head)
    )


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
    await _prune_stale_venvs(deps)
    await _report_disk_capacity(deps)
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
        elif personalization_result == "updated":
            result = "personalization_updated"
        if await _check_local_head_drift(
            settings.project_root,
            state,
            repo_ctx,
            deps,
            auto_deploy=settings.scheduler.auto_deploy,
        ) or await check_origin_drift(
            settings.project_root,
            state,
            repo_ctx,
            deps,
            auto_deploy=settings.scheduler.auto_deploy,
        ):
            result = "deploy_started"
        elif _local_code_awaits_approval(
            state,
            auto_deploy=settings.scheduler.auto_deploy,
        ):
            result = "update_pending"
        elif (await get_deployment_state()).pending is not None:
            result = "deploy_pending"
        else:
            refresh = await refresh_host_config(state.config_hash)
            if refresh.status is ConfigRefreshStatus.RESTART_REQUIRED:
                await deps.trigger_deploy(
                    state.deployed_sha,
                    rebuild=False,
                    config_hash=refresh.restart_hash,
                )
                result = "deploy_started"
            elif refresh.status is not ConfigRefreshStatus.UNCHANGED:
                result = refresh.status.value
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
