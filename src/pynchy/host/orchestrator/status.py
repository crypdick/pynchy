"""Operational status collector for the GET /status endpoint.

Pure data-collection module — no HTTP logic. Each subsystem has its own
private collector function. All I/O-bound checks run concurrently via
asyncio.gather() for fast response times (~200ms budget).
"""

from __future__ import annotations

import asyncio
import subprocess  # noqa: S404 - used for git subprocess exception types in status collection.
import time
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves GitStatusOperations annotations at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

import aiohttp
from temporalio.client import Client

from pynchy.host.orchestrator.capability_status import (
    CapabilityStatusOperations,
    collect_capability_status,
)
from pynchy.host.orchestrator.scheduled_work_status import collect_scheduled_work
from pynchy.host.orchestrator.speech_status import collect_speech_status
from pynchy.host.orchestrator.temporal.api import get_temporal_orchestration_states
from pynchy.logger import logger
from pynchy.plugins.speech.api import (  # noqa: TC001 - beartype resolves status annotations at runtime.
    SpeechSynthesizer,
)
from pynchy.runtime_names import runtime_container_name
from pynchy.scheduling.api import (  # noqa: TC001 - beartype resolves status annotations at runtime.
    HostJob,
    ScheduledTask,
)
from pynchy.state.api import (
    get_all_host_jobs,
    get_all_tasks,
    get_messaging_stats,
    get_router_state,
    get_task_run_logs,
)

_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GitStatusOperations:
    """Concrete source-control operations used by the status projection."""

    get_repo_context: Callable[[str], RepoStatusContext | None]
    get_head_sha: Callable[..., str]
    is_repo_dirty: Callable[..., bool]
    count_unpushed_commits: Callable[..., int]
    get_head_commit_message: Callable[..., str]
    detect_main_branch: Callable[..., str]
    run_git: Callable[..., subprocess.CompletedProcess[str]]


@runtime_checkable
class RepoStatusContext(Protocol):
    """Repository paths required by the status projection."""

    @property
    def root(self) -> Path: ...

    @property
    def worktrees_dir(self) -> Path: ...


def record_start_time() -> None:
    """Called once at service startup to record the wall-clock start time."""
    global _started_at  # noqa: PLW0603 - process-wide singleton.
    _started_at = datetime.now(UTC)


def get_temporal_scheduler_status() -> dict[str, Any]:
    """Return worker status lazily so status imports do not import the scheduler."""
    from pynchy.host.orchestrator.temporal.api import (  # noqa: PLC0415 - status module must not import the Temporal scheduler at module load.
        get_temporal_scheduler_status as _get_temporal_scheduler_status,
    )

    return _get_temporal_scheduler_status()


# ---------------------------------------------------------------------------
# StatusDeps protocol — injected by dep_factory
# ---------------------------------------------------------------------------


@runtime_checkable
class StatusDeps(Protocol):
    """Dependencies injected from app state for status collection."""

    repo_slugs: tuple[str, ...]
    temporal_address: str
    temporal_namespace: str
    temporal_task_queue: str
    capability_status_operations: CapabilityStatusOperations
    git_status: GitStatusOperations

    def is_shutting_down(self) -> bool: ...
    def get_channel_status(self) -> dict[str, bool]: ...
    def get_connection_status(self) -> dict[str, bool]: ...
    def get_queue_snapshot(self) -> dict[str, Any]: ...
    def get_gateway_info(self) -> dict[str, Any]: ...
    async def get_container_state(self, name: str) -> str: ...
    def get_active_sessions_count(self) -> int: ...
    def get_workspace_count(self) -> int: ...
    def get_speech_synthesizer(self) -> SpeechSynthesizer | None: ...
    async def get_canary_report(self, *, history_limit: int) -> dict[str, object]: ...


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
    connections = deps.get_connection_status()
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
        scheduled_work,
        gateway,
        temporal,
        canaries,
        speech,
    ) = await asyncio.gather(
        _collect_deploy(deps),
        asyncio.to_thread(_collect_repos, deps.repo_slugs, deps.git_status),
        _collect_messages(),
        _collect_scheduled_work(deps.temporal_address, deps.temporal_namespace),
        _collect_gateway(deps),
        _collect_temporal(
            deps.temporal_address,
            deps.temporal_namespace,
            deps.temporal_task_queue,
        ),
        deps.get_canary_report(history_limit=10),
        _collect_speech(deps.get_speech_synthesizer()),
    )
    tasks, host_jobs = scheduled_work
    canary_report = cast("dict[str, object]", canaries)
    capabilities = await collect_capability_status(
        canary_report,
        operations=deps.capability_status_operations,
    )

    return {
        "service": service,
        "deploy": deploy,
        "channels": channels,
        "connections": connections,
        "gateway": gateway,
        "queue": queue,
        "repos": repos,
        "messages": messages,
        "tasks": tasks,
        "host_jobs": host_jobs,
        "temporal": temporal,
        "canaries": canaries,
        "capabilities": capabilities,
        "speech": speech,
        "groups": groups,
    }


async def collect_status_summary(deps: StatusDeps, start_time_monotonic: float) -> dict[str, Any]:
    """Gather only stable operator fields; avoid deep diagnostic collection."""
    return {
        "service": _collect_service(deps, start_time_monotonic),
        "deploy": await _collect_deploy(deps),
        "queue": deps.get_queue_snapshot(),
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


async def _collect_speech(synthesizer: SpeechSynthesizer | None) -> dict[str, Any]:
    return await collect_speech_status(synthesizer)


async def _collect_deploy(deps: StatusDeps) -> dict[str, Any]:
    """Deploy info — git subprocesses + DB reads."""
    git_status = deps.git_status
    sha, dirty, unpushed, commit_msg, last_deploy_at, last_deploy_sha = await asyncio.gather(
        asyncio.to_thread(git_status.get_head_sha),
        asyncio.to_thread(git_status.is_repo_dirty),
        asyncio.to_thread(git_status.count_unpushed_commits),
        asyncio.to_thread(git_status.get_head_commit_message),
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


def _collect_repos(
    repo_slugs: tuple[str, ...],
    git_status: GitStatusOperations,
) -> dict[str, Any]:
    """Repo and worktree status — blocking git subprocesses.

    Called inside asyncio.to_thread() by the orchestrator.
    """
    result: dict[str, Any] = {}

    for slug in repo_slugs:
        repo_ctx = git_status.get_repo_context(slug)
        if repo_ctx is None or not repo_ctx.root.exists():
            continue
        result[slug] = _repo_status(repo_ctx, git_status)

    return result


def _repo_status(
    repo_ctx: RepoStatusContext,
    git_status: GitStatusOperations,
) -> dict[str, Any]:
    """Status for a single tracked repo, including its worktrees."""
    root = repo_ctx.root
    data: dict[str, Any] = {
        "head_sha": git_status.get_head_sha(cwd=root),
        "dirty": git_status.is_repo_dirty(cwd=root),
        "unpushed_commits": git_status.count_unpushed_commits(cwd=root),
    }

    # Enumerate worktrees
    worktrees_dir = repo_ctx.worktrees_dir
    if worktrees_dir.is_dir():
        main_branch = git_status.detect_main_branch(cwd=root)
        wt_data: dict[str, Any] = {}
        for wt_path in sorted(worktrees_dir.iterdir()):
            if wt_path.is_dir():
                wt_data[wt_path.name] = _worktree_status(
                    wt_path,
                    main_branch,
                    root,
                    git_status,
                )
        if wt_data:
            data["worktrees"] = wt_data

    return data


def _worktree_status(
    worktree_path: Path,
    main_branch: str,
    repo_root: Path,
    git_status: GitStatusOperations,
) -> dict[str, Any]:
    """Status for a single git worktree."""
    sha = git_status.get_head_sha(cwd=worktree_path)
    dirty = git_status.is_repo_dirty(cwd=worktree_path)
    branch = f"worktree/{worktree_path.name}"

    # Ahead/behind relative to main
    ahead_result = git_status.run_git(
        "rev-list", f"{main_branch}..{branch}", "--count", cwd=repo_root
    )
    behind_result = git_status.run_git(
        "rev-list", f"{branch}..{main_branch}", "--count", cwd=repo_root
    )

    ahead = int(ahead_result.stdout.strip()) if ahead_result.returncode == 0 else None
    behind = int(behind_result.stdout.strip()) if behind_result.returncode == 0 else None

    # Conflict detection: check for MERGE_HEAD or REBASE_HEAD in the actual git dir
    conflict = False
    try:
        git_dir_result = git_status.run_git("rev-parse", "--git-dir", cwd=worktree_path)
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


async def _collect_scheduled_work(
    temporal_address: str,
    temporal_namespace: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return await collect_scheduled_work(
        get_all_tasks,
        get_all_host_jobs,
        lambda task_id: get_task_run_logs(task_id, limit=5),
        _get_temporal_orchestration_states,
        (temporal_address, temporal_namespace),
    )


async def _get_temporal_orchestration_states(
    tasks: list[ScheduledTask],
    jobs: list[HostJob],
    temporal_address: str,
    temporal_namespace: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Preserve an injection seam around Temporal-backed scheduled-work status."""
    return await get_temporal_orchestration_states(
        tasks, jobs, temporal_address, temporal_namespace
    )


async def _collect_temporal(
    temporal_address: str,
    temporal_namespace: str,
    temporal_task_queue: str,
) -> dict[str, Any]:
    """Temporal scheduler status — config, cluster health, and worker state."""
    cluster = await _check_temporal_cluster_health(
        temporal_address,
        temporal_namespace,
    )
    return {
        "address": temporal_address,
        "namespace": temporal_namespace,
        "task_queue": temporal_task_queue,
        "cluster_healthy": cluster["healthy"],
        "cluster_error": cluster["error"],
        **get_temporal_scheduler_status(),
    }


async def _check_temporal_cluster_health(address: str, namespace: str) -> dict[str, Any]:
    """Return WorkflowService health using Temporal's gRPC health service."""
    try:
        client = await Client.connect(address, namespace=namespace, lazy=True)
        healthy = await client.service_client.check_health(timeout=timedelta(seconds=2))
    except Exception as exc:  # noqa: BLE001 - allow: exception-handling; degraded status is intentional.
        logger.debug(
            "Temporal cluster health check failed",
            address=address,
            namespace=namespace,
            err=str(exc),
        )
        return {"healthy": None, "error": str(exc)}
    return {"healthy": healthy, "error": None}


async def _collect_gateway(deps: StatusDeps) -> dict[str, Any]:
    """Gateway health from Docker and LiteLLM readiness checks.

    Args:
        deps: Runtime state and container-status operation.
    """
    info = deps.get_gateway_info()
    result: dict[str, Any] = {
        "mode": info.get("mode", "unknown"),
        "redaction": info.get("redaction", "unknown"),
    }
    if info.get("mode") != "litellm":
        return result

    if info.get("managed", True):
        litellm_state, pg_state = await asyncio.gather(
            deps.get_container_state(runtime_container_name("litellm")),
            deps.get_container_state(runtime_container_name("litellm-db")),
        )
        result["litellm_container"] = litellm_state
        result["postgres_container"] = pg_state

    # LiteLLM documents /health/readiness as the proxy readiness endpoint;
    # /health performs provider model calls and can be provider-shape-sensitive.
    # Keep status non-inferential: explicit runtime tests and real requests prove routes.
    port = info.get("port")
    key = info.get("key")
    if port and key:
        result.update(await _check_litellm_readiness(port, key))

    return result


async def _check_litellm_readiness(port: int, key: str) -> dict[str, Any]:
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"http://localhost:{port}/health/readiness",
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001 - gateway health is best-effort status collection.
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
