"""Operational status collector for the GET /status endpoint.

Pure data-collection module — no HTTP logic. Each subsystem has its own
private collector function. All I/O-bound checks run concurrently via
asyncio.gather() for fast response times (~200ms budget).
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pynchy.config import get_settings
from pynchy.host.container_manager.docker import run_docker
from pynchy.state import (
    get_all_host_jobs,
    get_all_tasks,
    get_messaging_stats,
    get_router_state,
)
from pynchy.git_ops.repo import RepoContext, get_repo_context
from pynchy.git_ops.utils import (
    count_unpushed_commits,
    detect_main_branch,
    get_head_commit_message,
    get_head_sha,
    is_repo_dirty,
    run_git,
)
from pynchy.logger import logger

# Module-level wall-clock start time for uptime reporting.
# Monotonic _start_time in http_server.py is for duration math only;
# we need a real timestamp for the "started_at" field.
_started_at: datetime | None = None


def record_start_time() -> None:
    """Called once at service startup to record the wall-clock start time."""
    global _started_at
    _started_at = datetime.now(UTC)


# ---------------------------------------------------------------------------
# StatusDeps protocol — injected by dep_factory
# ---------------------------------------------------------------------------


class StatusDeps(Protocol):
    """Dependencies injected from app state for status collection."""

    def is_shutting_down(self) -> bool: ...
    def get_channel_status(self) -> dict[str, bool]: ...
    def get_queue_snapshot(self) -> dict[str, Any]: ...
    def get_gateway_info(self) -> dict[str, Any]: ...
    def get_active_sessions_count(self) -> int: ...
    def get_workspace_count(self) -> int: ...


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def collect_status(deps: StatusDeps, start_time_monotonic: float) -> dict[str, Any]:
    """Gather operational status from all subsystems.

    Fires all independent checks concurrently for fast response.

    Args:
        deps: Injected runtime dependencies (in-memory state).
        start_time_monotonic: The monotonic timestamp from http_server._start_time.
    """
    # In-memory reads (instant)
    service = _collect_service(deps, start_time_monotonic)
    channels = deps.get_channel_status()
    queue = deps.get_queue_snapshot()
    groups = {
        "total": deps.get_workspace_count(),
        "active_sessions": deps.get_active_sessions_count(),
    }

    # Concurrent I/O: DB queries, git subprocesses, gateway health
    (
        deploy,
        repos,
        messages,
        tasks,
        host_jobs,
        gateway,
    ) = await asyncio.gather(
        _collect_deploy(),
        asyncio.to_thread(_collect_repos),
        _collect_messages(),
        _collect_tasks(),
        _collect_host_jobs(),
        _collect_gateway(deps.get_gateway_info()),
    )

    return {
        "service": service,
        "deploy": deploy,
        "channels": channels,
        "gateway": gateway,
        "queue": queue,
        "repos": repos,
        "messages": messages,
        "tasks": tasks,
        "host_jobs": host_jobs,
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# Per-section collectors
# ---------------------------------------------------------------------------


def _collect_service(deps: StatusDeps, start_time_monotonic: float) -> dict[str, Any]:
    """Service health — all in-memory."""
    status = "shutting_down" if deps.is_shutting_down() else "ok"
    return {
        "status": status,
        "started_at": _started_at.isoformat() if _started_at else None,
        "uptime_seconds": round(time.monotonic() - start_time_monotonic),
    }


async def _collect_deploy() -> dict[str, Any]:
    """Deploy info — git subprocesses + DB reads."""
    sha, dirty, unpushed, commit_msg, last_deploy_at, last_deploy_sha = await asyncio.gather(
        asyncio.to_thread(get_head_sha),
        asyncio.to_thread(is_repo_dirty),
        asyncio.to_thread(count_unpushed_commits),
        asyncio.to_thread(get_head_commit_message),
        get_router_state("last_deploy_at"),
        get_router_state("last_deploy_sha"),
    )
    return {
        "head_sha": sha,
        "head_commit": commit_msg,
        "dirty": dirty,
        "unpushed_commits": unpushed,
        "last_deploy_at": last_deploy_at,
        "last_deploy_sha": last_deploy_sha,
    }


def _collect_repos() -> dict[str, Any]:
    """Repo and worktree status — blocking git subprocesses.

    Called inside asyncio.to_thread() by the orchestrator.
    """
    s = get_settings()
    result: dict[str, Any] = {}

    for slug in s.repos:
        repo_ctx = get_repo_context(slug)
        if repo_ctx is None or not repo_ctx.root.exists():
            continue
        result[slug] = _repo_status(repo_ctx)

    return result


def _repo_status(repo_ctx: RepoContext) -> dict[str, Any]:
    """Status for a single tracked repo, including its worktrees."""
    root = repo_ctx.root
    data: dict[str, Any] = {
        "head_sha": get_head_sha(cwd=root),
        "dirty": is_repo_dirty(cwd=root),
        "unpushed_commits": count_unpushed_commits(cwd=root),
    }

    # Enumerate worktrees
    worktrees_dir = repo_ctx.worktrees_dir
    if worktrees_dir.is_dir():
        main_branch = detect_main_branch(cwd=root)
        wt_data: dict[str, Any] = {}
        for wt_path in sorted(worktrees_dir.iterdir()):
            if wt_path.is_dir():
                wt_data[wt_path.name] = _worktree_status(wt_path, main_branch, root)
        if wt_data:
            data["worktrees"] = wt_data

    return data


def _worktree_status(worktree_path: Path, main_branch: str, repo_root: Path) -> dict[str, Any]:
    """Status for a single git worktree."""
    sha = get_head_sha(cwd=worktree_path)
    dirty = is_repo_dirty(cwd=worktree_path)
    branch = f"worktree/{worktree_path.name}"

    # Ahead/behind relative to main
    ahead_result = run_git("rev-list", f"{main_branch}..{branch}", "--count", cwd=repo_root)
    behind_result = run_git("rev-list", f"{branch}..{main_branch}", "--count", cwd=repo_root)

    ahead = int(ahead_result.stdout.strip()) if ahead_result.returncode == 0 else None
    behind = int(behind_result.stdout.strip()) if behind_result.returncode == 0 else None

    # Conflict detection: check for MERGE_HEAD or REBASE_HEAD in the actual git dir
    conflict = False
    try:
        git_dir_result = run_git("rev-parse", "--git-dir", cwd=worktree_path)
        if git_dir_result.returncode == 0:
            gd = Path(git_dir_result.stdout.strip())
            if not gd.is_absolute():
                gd = worktree_path / gd
            conflict = (gd / "MERGE_HEAD").exists() or (gd / "REBASE_HEAD").exists()
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Conflict detection failed", worktree=str(worktree_path), err=str(exc))

    return {
        "sha": sha,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
        "conflict": conflict,
    }


async def _collect_messages() -> dict[str, Any]:
    """Message stats — delegated to db.get_messaging_stats()."""
    return await get_messaging_stats()


async def _collect_tasks() -> list[dict[str, Any]]:
    """Scheduled task list — async DB."""
    tasks = await get_all_tasks()
    return [
        {
            "id": t.id,
            "group": t.group_folder,
            "schedule_type": t.schedule_type,
            "schedule_value": t.schedule_value,
            "status": t.status,
            "next_run": t.next_run,
            "last_run": t.last_run,
            "last_result": t.last_result,
        }
        for t in tasks
    ]


async def _collect_host_jobs() -> list[dict[str, Any]]:
    """Host job list — async DB."""
    jobs = await get_all_host_jobs()
    return [
        {
            "id": j.id,
            "name": j.name,
            "schedule_type": j.schedule_type,
            "schedule_value": j.schedule_value,
            "status": j.status,
            "enabled": j.enabled,
            "next_run": j.next_run,
            "last_run": j.last_run,
        }
        for j in jobs
    ]


async def _collect_gateway(info: dict[str, Any]) -> dict[str, Any]:
    """Gateway health — Docker inspect + HTTP health check.

    Args:
        info: Dict from deps.get_gateway_info() with mode, port, key.
    """
    result: dict[str, Any] = {"mode": info.get("mode", "unknown")}

    if info.get("mode") != "litellm":
        return result

    litellm_state, pg_state = await asyncio.gather(
        _container_state("pynchy-litellm"),
        _container_state("pynchy-litellm-db"),
    )
    result["litellm_container"] = litellm_state
    result["postgres_container"] = pg_state

    # LiteLLM /health HTTP check
    port = info.get("port")
    key = info.get("key")
    if port and key:
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"http://localhost:{port}/health",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
                data = await resp.json()
                result["healthy_models"] = data.get("healthy_count", 0)
                result["unhealthy_models"] = data.get("unhealthy_count", 0)
        except Exception as exc:
            logger.debug("Gateway health check failed", err=str(exc))
            result["healthy_models"] = None
            result["unhealthy_models"] = None

    return result


async def _container_state(name: str) -> str:
    """Return 'running', 'stopped', or 'not_found' for a Docker container."""
    try:
        result = await run_docker("inspect", "-f", "{{.State.Status}}", name, check=False)
        if result.returncode != 0:
            return "not_found"
        return result.stdout.strip()  # "running", "exited", "created", etc.
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # TimeoutExpired: docker CLI hung; FileNotFoundError: docker not installed.
        # Both are expected in degraded environments — return not_found.
        return "not_found"
