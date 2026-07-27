"""Host-side cleanup for orphaned agent containers."""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - cleanup catches fixed-runtime subprocess failures.
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from pynchy.host.container_manager.runtime_names import runtime_namespace
from pynchy.host.container_manager.session import active_session_container_names
from pynchy.logger import logger
from pynchy.plugins.runtimes.detection import get_runtime
from pynchy.types import OrphanReapAgeMs  # noqa: TC001, RUF100 - public cleanup contract

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
    return cast("list[RuntimeContainerRecord]", list_containers(f"{runtime_namespace()}-"))


def _remove_runtime_container(runtime: object, name: str) -> bool:
    remove_container = getattr(runtime, "remove_container", None)
    if not callable(remove_container):
        return False
    return bool(remove_container(name, force=True))


def cleanup_runtime_builder(runtime: object) -> bool:
    """Let runtimes discard build-only state after Pynchy image builds."""
    cleanup_builder = getattr(runtime, "cleanup_builder", None)
    if not callable(cleanup_builder):
        return True
    try:
        cleaned = cleanup_builder()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Failed to clean container builder", err=str(exc))
        return False
    if cleaned is False:
        logger.warning("Failed to clean container builder")
        return False
    return True


def cleanup_runtime_images(runtime: object) -> bool:
    """Let runtimes discard dangling image layers without deleting tagged images."""
    prune_images = getattr(runtime, "prune_images", None)
    if not callable(prune_images):
        return False
    try:
        pruned = bool(prune_images(all_images=False))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Failed to prune container images", err=str(exc))
        return False
    if not pruned:
        logger.warning("Failed to prune container images")
    return pruned


def cleanup_runtime_build_state(runtime: object) -> bool:
    """Discard stale builders and dangling layers around controlled builds."""
    builder_cleaned = cleanup_runtime_builder(runtime)
    images_cleaned = cleanup_runtime_images(runtime)
    return builder_cleaned and images_cleaned


def reap_orphaned_agent_containers(
    *,
    orphan_age_ms: OrphanReapAgeMs,
    runtime: object | None = None,
    active_names: set[str] | None = None,
) -> list[ReapedContainer]:
    """Remove agent containers that are not owned by a live session."""

    resolved_runtime = runtime if runtime is not None else get_runtime()
    protected = active_names if active_names is not None else active_session_container_names()
    retention = timedelta(milliseconds=max(0, orphan_age_ms))
    now = datetime.now(UTC)

    try:
        containers = _list_runtime_containers(resolved_runtime)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Failed to list orphaned agent containers", err=str(exc))
        return []

    reaped: list[ReapedContainer] = []
    for container in containers:
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
        except (OSError, subprocess.SubprocessError) as exc:
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
