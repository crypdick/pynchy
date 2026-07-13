"""Temporal activities for host and external repository sync polling."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from typing import cast

from temporalio import activity

from pynchy.config import get_settings
from pynchy.host.git_ops._worktree_notify import host_notify_worktree_updates, last_notified_sha
from pynchy.host.git_ops.repo import get_repo_context
from pynchy.host.git_ops.sync_poll import (
    _check_local_head_drift,
    _check_origin_drift,
    _find_pynchy_repo_ctx,
    _hash_config_files,
    _host_get_origin_main_sha,
    _HostSyncState,
    get_local_head_sha,
    host_update_main,
)
from pynchy.host.git_ops.utils import git_env_with_token
from pynchy.host.orchestrator.adapters import SessionManager, find_admin_jid
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.logger import logger
from pynchy.state import get_router_state, set_router_state
from pynchy.types import (
    WorkspaceProfile,  # noqa: TC001, RUF100 - beartype resolves Temporal git-sync annotations at runtime.
)

HOST_GIT_SYNC_ID = "git-sync-host"
EXTERNAL_GIT_SYNC_PREFIX = "git-sync-repo:"
_HOST_STATE_KEY = "temporal_git_sync_host_state"
_EXTERNAL_STATE_PREFIX = "temporal_git_sync_external_state:"
_RUNTIME_HARNESS_ENV = "PYNCHY_RUNTIME_HARNESS"


def _workspace_map(deps: object) -> dict[str, WorkspaceProfile]:
    workspaces = getattr(deps, "workspaces", {})
    workspaces = workspaces() if callable(workspaces) else workspaces
    return cast("dict[str, WorkspaceProfile]", workspaces)


class _TemporalGitSyncDeps:
    """Adapter that lets existing git-sync helpers start Temporal deploys."""

    def __init__(self, deps: object, *, reason: str) -> None:
        self._deps = deps
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

    def _active_sessions(self) -> dict[str, str]:
        if hasattr(self._deps, "get_active_sessions"):
            active_sessions = self._deps.get_active_sessions()
            if isinstance(active_sessions, dict):
                return {str(jid): str(session) for jid, session in active_sessions.items()}
            return {}
        manager = SessionManager(
            getattr(self._deps, "sessions", {}),
            getattr(self._deps, "session_cleared", set()),
        )
        return manager.get_active_sessions(self.workspaces())

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
        from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415, RUF100 - avoids scheduler <-> git_sync import cycle.
            start_deploy_workflow,
        )

        workspaces = self.workspaces()
        request = DeployRequest(
            chat_jid=find_admin_jid(workspaces),
            commit_sha=get_local_head_sha(get_settings().project_root),
            previous_sha=previous_sha,
            active_sessions=self._active_sessions(),
            rebuild=rebuild,
            reason=self._reason,
        )
        await start_deploy_workflow(request)


async def _load_host_state() -> _HostSyncState:
    settings = get_settings()
    raw = await get_router_state(_HOST_STATE_KEY)
    if raw:
        try:
            payload = json.loads(raw)
            return _HostSyncState(
                last_origin_sha=payload.get("last_origin_sha"),
                deployed_sha=str(payload.get("deployed_sha", "")),
                config_hash=str(payload.get("config_hash", "")),
                local_head=payload.get("local_head"),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Corrupt Temporal host git-sync state, reinitializing")

    return _HostSyncState(
        last_origin_sha=await asyncio.to_thread(_host_get_origin_main_sha, settings.project_root),
        deployed_sha=await asyncio.to_thread(get_local_head_sha, settings.project_root),
        config_hash=await asyncio.to_thread(_hash_config_files),
    )


async def _save_host_state(state: _HostSyncState) -> None:
    await set_router_state(_HOST_STATE_KEY, json.dumps(asdict(state)))


async def _config_drift_started_deploy(state: _HostSyncState, deps: _TemporalGitSyncDeps) -> bool:
    current_config_hash = await asyncio.to_thread(_hash_config_files)
    if current_config_hash == state.config_hash:
        return False
    logger.info("Config files changed, starting Temporal deploy")
    await deps.trigger_deploy(state.deployed_sha, rebuild=False)
    state.config_hash = current_config_hash
    return True


@activity.defn(name="run_host_git_sync")
async def run_host_git_sync() -> str:
    """Run one host-repo git sync poll through Temporal."""
    if os.environ.get(_RUNTIME_HARNESS_ENV) == "1":
        # The hermetic runtime owns its process lifecycle directly. Its
        # generated config is expected to change across feature work, so the
        # production deploy handoff would SIGTERM an unsupervised test process.
        _record_activity_result(HOST_GIT_SYNC_ID, "skipped")
        return "skipped"

    deps = _TemporalGitSyncDeps(_require_scheduler_deps(), reason="host_git_sync")
    settings = get_settings()
    state = await _load_host_state()
    repo_ctx = _find_pynchy_repo_ctx(settings, settings.project_root)
    result = "idle"

    try:
        if (
            await _config_drift_started_deploy(state, deps)
            or await _check_local_head_drift(settings.project_root, state, repo_ctx, deps)
            or await _check_origin_drift(settings.project_root, state, repo_ctx, deps)
        ):
            result = "deploy_started"
    finally:
        if result == "deploy_started":
            state.deployed_sha = await asyncio.to_thread(get_local_head_sha, settings.project_root)
            state.config_hash = await asyncio.to_thread(_hash_config_files)
        await _save_host_state(state)

    _record_activity_result(HOST_GIT_SYNC_ID, result)
    return result


async def _load_external_origin(repo_slug: str) -> str | None:
    return await get_router_state(f"{_EXTERNAL_STATE_PREFIX}{repo_slug}")


async def _save_external_origin(repo_slug: str, origin_sha: str) -> None:
    await set_router_state(f"{_EXTERNAL_STATE_PREFIX}{repo_slug}", origin_sha)


@activity.defn(name="run_external_git_sync")
async def run_external_git_sync(repo_slug: str) -> str:
    """Run one external-repo sync poll through Temporal."""
    repo_ctx = get_repo_context(repo_slug)
    if repo_ctx is None:
        _record_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "skipped")
        return "skipped"

    deps = _TemporalGitSyncDeps(_require_scheduler_deps(), reason="external_git_sync")
    env = git_env_with_token(repo_slug)
    current_origin = await asyncio.to_thread(_host_get_origin_main_sha, repo_ctx.root, env)
    if not current_origin:
        _record_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "unavailable")
        return "unavailable"

    last_origin = await _load_external_origin(repo_slug)
    if not last_origin:
        await _save_external_origin(repo_slug, current_origin)
        _record_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "initialized")
        return "initialized"
    if current_origin == last_origin:
        _record_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "idle")
        return "idle"

    updated = await asyncio.to_thread(host_update_main, repo_ctx.root, env)
    if not updated:
        _record_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "sync_failed")
        return "sync_failed"

    await _save_external_origin(repo_slug, current_origin)
    new_head = await asyncio.to_thread(get_local_head_sha, repo_ctx.root)
    if last_notified_sha.get(str(repo_ctx.root), "") != new_head:
        await host_notify_worktree_updates(None, deps, repo_ctx)

    _record_activity_result(f"{EXTERNAL_GIT_SYNC_PREFIX}{repo_slug}", "synced")
    return "synced"
