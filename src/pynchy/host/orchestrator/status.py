"""Operational status collector for the GET /status endpoint.

Pure data-collection module — no HTTP logic. Each subsystem has its own
private collector function. All I/O-bound checks run concurrently via
asyncio.gather() for fast response times (~200ms budget).
"""

from __future__ import annotations

import asyncio
import subprocess  # noqa: S404, RUF100 - used for subprocess exception types in status collection.
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from temporalio.client import Client

from pynchy.config import get_settings
from pynchy.host.container_manager.docker import run_docker
from pynchy.host.container_manager.onecli import collect_onecli_status
from pynchy.host.git_ops.repo import RepoContext, get_repo_context
from pynchy.host.git_ops.utils import (
    count_unpushed_commits,
    detect_main_branch,
    get_head_commit_message,
    get_head_sha,
    is_repo_dirty,
    run_git,
)
from pynchy.logger import logger
from pynchy.state import (
    get_all_host_jobs,
    get_all_tasks,
    get_messaging_stats,
    get_router_state,
    get_task_run_logs,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves status annotations at runtime.
    TaskRunLog,
)


@dataclass
class _StatusState:
    started_at: datetime | None = None


_state = _StatusState()


def record_start_time() -> None:
    """Called once at service startup to record the wall-clock start time."""
    _state.started_at = datetime.now(UTC)


def get_temporal_scheduler_status() -> dict[str, Any]:
    """Return worker status lazily so status imports do not import the scheduler."""
    from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415, RUF100 - status module must not import the Temporal scheduler at module load.
        get_temporal_scheduler_status as _get_temporal_scheduler_status,
    )

    return _get_temporal_scheduler_status()


# ---------------------------------------------------------------------------
# StatusDeps protocol — injected by dep_factory
# ---------------------------------------------------------------------------


@runtime_checkable
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
        onecli,
        temporal,
    ) = await asyncio.gather(
        _collect_deploy(),
        asyncio.to_thread(_collect_repos),
        _collect_messages(),
        _collect_tasks(),
        _collect_host_jobs(),
        _collect_gateway(deps.get_gateway_info()),
        asyncio.to_thread(collect_onecli_status),
        _collect_temporal(),
    )

    return {
        "service": service,
        "deploy": deploy,
        "channels": channels,
        "gateway": gateway,
        "onecli": onecli,
        "queue": queue,
        "repos": repos,
        "messages": messages,
        "tasks": tasks,
        "host_jobs": host_jobs,
        "temporal": temporal,
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
        "started_at": _state.started_at.isoformat() if _state.started_at else None,
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
    result: dict[str, Any] = {}

    for slug in _configured_repo_slugs():
        repo_ctx = get_repo_context(slug)
        if repo_ctx is None or not repo_ctx.root.exists():
            continue
        result[slug] = _repo_status(repo_ctx)

    return result


def _configured_repo_slugs() -> list[str]:
    s = get_settings()
    slugs: list[str] = []
    seen: set[str] = set()

    def add(slug: str) -> None:
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)

    for slug in getattr(s.repos, "overrides", {}):
        add(slug)
    for profile in getattr(s, "profiles", {}).values():
        for slug in profile.repo:
            add(slug)
    return slugs


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
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Conflict detection failed", worktree=str(worktree_path), err=str(exc))
    else:
        if git_dir_result.returncode == 0:
            gd = Path(git_dir_result.stdout.strip())
            if not gd.is_absolute():
                gd = worktree_path / gd
            conflict = (gd / "MERGE_HEAD").exists() or (gd / "REBASE_HEAD").exists()

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
    task_logs = await asyncio.gather(
        *(get_task_run_logs(t.id, limit=5) for t in tasks),
    )
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
            "run_health": _task_run_health(logs),
        }
        for t, logs in zip(tasks, task_logs, strict=True)
    ]


def _task_run_health(logs: list[TaskRunLog]) -> dict[str, Any]:
    """Summarize recent scheduled-task attempts for operator status."""
    last = logs[0] if logs else None
    consecutive_failures = 0
    for log in logs:
        if log.status != "error":
            break
        consecutive_failures += 1

    return {
        "last_status": last.status if last else None,
        "consecutive_failures": consecutive_failures,
        "last_error_signature": last.error_signature if last else None,
        "last_temporal_workflow_id": last.temporal_workflow_id if last else None,
        "last_temporal_attempt": last.temporal_attempt if last else None,
        "escalation_reason": last.escalation_reason if last else None,
    }


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


async def _collect_temporal() -> dict[str, Any]:
    """Temporal scheduler status — config, cluster health, and worker state."""
    scheduler = get_settings().scheduler
    cluster = await _check_temporal_cluster_health(
        scheduler.temporal_address,
        scheduler.temporal_namespace,
    )
    return {
        "address": scheduler.temporal_address,
        "namespace": scheduler.temporal_namespace,
        "task_queue": scheduler.temporal_task_queue,
        "cluster_healthy": cluster["healthy"],
        "cluster_error": cluster["error"],
        **get_temporal_scheduler_status(),
    }


async def _check_temporal_cluster_health(address: str, namespace: str) -> dict[str, Any]:
    """Return WorkflowService health using Temporal's gRPC health service."""
    try:
        client = await Client.connect(address, namespace=namespace, lazy=True)
        healthy = await client.service_client.check_health(timeout=timedelta(seconds=2))
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; degraded status is intentional.
        logger.debug(
            "Temporal cluster health check failed",
            address=address,
            namespace=namespace,
            err=str(exc),
        )
        return {"healthy": None, "error": str(exc)}
    return {"healthy": healthy, "error": None}


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

    # LiteLLM documents /health/readiness as the proxy readiness endpoint;
    # /health performs provider model calls and can be provider-shape-sensitive.
    port = info.get("port")
    key = info.get("key")
    if port and key:
        result.update(await _check_litellm_readiness(port, key))

    return result


async def _check_litellm_readiness(port: int, key: str) -> dict[str, Any]:
    try:
        import aiohttp  # noqa: PLC0415, RUF100 - gateway readiness is optional best-effort status collection.
    except Exception as exc:  # noqa: BLE001, RUF100 - gateway health is best-effort status collection.
        logger.debug("Gateway health check failed", err=str(exc))
        return {"ready": None}

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"http://localhost:{port}/health/readiness",
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001, RUF100 - gateway health is best-effort status collection.
        logger.debug("Gateway health check failed", err=str(exc))
        return {"ready": None}

    result: dict[str, Any] = {
        "ready": (
            resp.status == 200
            and data.get("status") in {"connected", "healthy"}
            and data.get("db") == "connected"
        ),
        "database": data.get("db"),
    }
    if "litellm_version" in data:
        result["litellm_version"] = data["litellm_version"]
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
