"""Host-side cleanup for orphaned agent containers."""

from __future__ import annotations

import subprocess  # noqa: S404 - cleanup catches fixed-runtime subprocess failures.
from collections.abc import (
    Sequence,
)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from pynchy.identifiers import OrphanReapAgeMs
from pynchy.logger import logger
from pynchy.runtime_names import runtime_namespace

_LIVE_STATES = {"running", "paused", "restarting"}
_DISPOSABLE_STATES = {"created", "dead", "exited", "stopped"}


@runtime_checkable
class RuntimeContainerRecord(Protocol):
    name: str
    state: str
    image: str
    created_at: datetime | None

    @property
    def is_agent_container(self) -> bool: ...


@runtime_checkable
class OrphanReapingRuntime(Protocol):
    """Container operations required to reap unowned agent containers."""

    def list_containers(self, prefix: str = "pynchy-") -> Sequence[RuntimeContainerRecord]: ...

    def remove_container(self, name: str, *, force: bool = True) -> bool: ...


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


def _list_runtime_containers(runtime: OrphanReapingRuntime) -> Sequence[RuntimeContainerRecord]:
    return runtime.list_containers(f"{runtime_namespace()}-")


def _remove_runtime_container(runtime: OrphanReapingRuntime, name: str) -> bool:
    return runtime.remove_container(name, force=True)


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
    """Prune dangling image layers without discarding a reusable builder cache."""
    return cleanup_runtime_images(runtime)


def reap_orphaned_agent_containers(
    *,
    orphan_age_ms: OrphanReapAgeMs,
    runtime: OrphanReapingRuntime,
    active_names: set[str],
) -> list[ReapedContainer]:
    """Remove agent containers that are not owned by a live session."""

    retention = timedelta(milliseconds=max(0, orphan_age_ms))
    now = datetime.now(UTC)

    try:
        containers = _list_runtime_containers(runtime)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Failed to list orphaned agent containers", err=str(exc))
        return []

    reaped: list[ReapedContainer] = []
    for container in containers:
        should_reap, reason = _is_stale(
            container,
            active_names=active_names,
            now=now,
            retention=retention,
        )
        if not should_reap:
            continue
        try:
            removed = _remove_runtime_container(runtime, container.name)
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
