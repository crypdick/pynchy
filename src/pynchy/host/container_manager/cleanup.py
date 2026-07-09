"""Host-side cleanup for orphaned agent containers."""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - cleanup catches fixed-runtime subprocess failures.
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.plugins.runtimes.detection import get_runtime

_LIVE_STATES = {"running", "paused", "restarting"}
_DISPOSABLE_STATES = {"created", "dead", "exited", "stopped"}


class RuntimeContainerRecord(Protocol):
    name: str
    state: str
    image: str
    created_at: datetime | None

    @property
    def is_agent_container(self) -> bool: ...


@dataclass(frozen=True)
class ReapedContainer:
    name: str
    state: str
    reason: str


def _created_at_utc(created_at: datetime | None) -> datetime | None:
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=UTC)
    return created_at.astimezone(UTC)


def _is_stale(
    container: RuntimeContainerRecord,
    *,
    active_names: set[str],
    now: datetime,
    retention: timedelta,
) -> tuple[bool, str]:
    if not container.is_agent_container:
        return False, "not-agent"
    if container.name in active_names:
        return False, "active-session"

    state = container.state.lower()
    should_reap = False
    reason = "within-retention"
    if state in _DISPOSABLE_STATES:
        should_reap = True
        reason = state
    elif state not in _LIVE_STATES:
        reason = f"state:{state or 'unknown'}"
    else:
        created_at = _created_at_utc(container.created_at)
        if created_at is None:
            reason = "unknown-age"
        elif now - created_at >= retention:
            should_reap = True
            reason = f"unowned-{state}"
    return should_reap, reason


def _list_runtime_containers(runtime: object) -> list[RuntimeContainerRecord]:
    list_containers = getattr(runtime, "list_containers", None)
    if not callable(list_containers):
        return []
    return cast("list[RuntimeContainerRecord]", list_containers("pynchy-"))


def _remove_runtime_container(runtime: object, name: str) -> bool:
    remove_container = getattr(runtime, "remove_container", None)
    if not callable(remove_container):
        return False
    return bool(remove_container(name, force=True))


def cleanup_runtime_builder(runtime: object) -> None:
    """Let runtimes discard build-only state after Pynchy image builds."""
    cleanup_builder = getattr(runtime, "cleanup_builder", None)
    if not callable(cleanup_builder):
        return
    try:
        cleanup_builder()
    except OSError as exc:
        logger.warning("Failed to clean container builder", err=str(exc))


def cleanup_runtime_images(runtime: object) -> bool:
    """Let runtimes discard dangling image layers without deleting tagged images."""
    prune_images = getattr(runtime, "prune_images", None)
    if not callable(prune_images):
        return False
    try:
        return bool(prune_images(all_images=False))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Failed to prune container images", err=str(exc))
        return False


def reap_orphaned_agent_containers(
    *,
    runtime: object | None = None,
    active_names: set[str] | None = None,
    orphan_age_ms: int | None = None,
) -> list[ReapedContainer]:
    """Remove agent containers that are not owned by a live session."""
    from pynchy.host.container_manager.session import active_session_container_names

    resolved_runtime = runtime if runtime is not None else get_runtime()
    protected = active_names if active_names is not None else active_session_container_names()
    retention_ms = orphan_age_ms
    if retention_ms is None:
        retention_ms = get_settings().container.orphan_reap_age_ms
    retention = timedelta(milliseconds=max(0, retention_ms))
    now = datetime.now(UTC)

    reaped: list[ReapedContainer] = []
    for container in _list_runtime_containers(resolved_runtime):
        should_reap, reason = _is_stale(
            container,
            active_names=protected,
            now=now,
            retention=retention,
        )
        if not should_reap:
            continue
        try:
            removed = _remove_runtime_container(resolved_runtime, container.name)
        except OSError as exc:
            logger.warning(
                "Failed to reap orphaned agent container",
                container=container.name,
                state=container.state,
                reason=reason,
                err=str(exc),
            )
            continue
        if not removed:
            logger.warning(
                "Failed to reap orphaned agent container",
                container=container.name,
                state=container.state,
                reason=reason,
            )
            continue
        reaped.append(ReapedContainer(container.name, container.state, reason))

    if reaped:
        logger.info(
            "Reaped orphaned agent containers",
            count=len(reaped),
            containers=[item.name for item in reaped],
        )
    return reaped
