"""Tests for orphaned agent container cleanup."""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - synthetic runtime timeout below.
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pynchy.plugins.runtimes.cleanup import (
    cleanup_runtime_images,
    reap_orphaned_agent_containers,
)
from pynchy.types import OrphanReapAgeMs

_IMMEDIATE_REAP = OrphanReapAgeMs(0)
_SEVEN_DAYS = OrphanReapAgeMs(604800000)


@dataclass
class FakeRuntimeContainer:
    name: str
    state: str
    image: str = "pynchy-agent:latest"
    created_at: datetime | None = None
    agent: bool = True

    @property
    def is_agent_container(self) -> bool:
        return self.agent


class FakeRuntime:
    def __init__(self, containers: list[FakeRuntimeContainer]) -> None:
        self.containers = containers
        self.removed: list[str] = []
        self.pruned_images: list[bool] = []

    def list_containers(self, prefix: str = "pynchy-") -> list[FakeRuntimeContainer]:
        return [item for item in self.containers if item.name.startswith(prefix)]

    def remove_container(self, name: str, *, force: bool = True) -> bool:
        assert force is True
        self.removed.append(name)
        return True

    def prune_images(self, *, all_images: bool = False) -> bool:
        self.pruned_images.append(all_images)
        return True


def test_reaps_stopped_agent_container_immediately() -> None:
    runtime = FakeRuntime(
        [
            FakeRuntimeContainer(
                "pynchy-admin",
                "stopped",
                created_at=datetime.now(UTC),
            )
        ]
    )

    result = reap_orphaned_agent_containers(
        runtime=runtime,
        active_names=set(),
        orphan_age_ms=_SEVEN_DAYS,
    )

    assert runtime.removed == ["pynchy-admin"]
    assert result[0].reason == "stopped"


def test_preserves_container_owned_by_active_session() -> None:
    runtime = FakeRuntime(
        [
            FakeRuntimeContainer(
                "pynchy-admin",
                "stopped",
                created_at=datetime.now(UTC) - timedelta(days=30),
            )
        ]
    )

    result = reap_orphaned_agent_containers(
        runtime=runtime,
        active_names={"pynchy-admin"},
        orphan_age_ms=_IMMEDIATE_REAP,
    )

    assert result == []
    assert runtime.removed == []


def test_preserves_recent_unowned_live_agent_container() -> None:
    runtime = FakeRuntime(
        [
            FakeRuntimeContainer(
                "pynchy-admin",
                "running",
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        ]
    )

    result = reap_orphaned_agent_containers(
        runtime=runtime,
        active_names=set(),
        orphan_age_ms=_SEVEN_DAYS,
    )

    assert result == []
    assert runtime.removed == []


def test_reaps_stale_unowned_live_agent_container() -> None:
    runtime = FakeRuntime(
        [
            FakeRuntimeContainer(
                "pynchy-admin",
                "paused",
                created_at=datetime.now(UTC) - timedelta(days=8),
            )
        ]
    )

    result = reap_orphaned_agent_containers(
        runtime=runtime,
        active_names=set(),
        orphan_age_ms=_SEVEN_DAYS,
    )

    assert runtime.removed == ["pynchy-admin"]
    assert result[0].reason == "unowned-paused"


def test_ignores_non_agent_pynchy_container() -> None:
    runtime = FakeRuntime(
        [
            FakeRuntimeContainer(
                "pynchy-litellm",
                "running",
                image="ghcr.io/berriai/litellm:main-latest",
                created_at=datetime.now(UTC) - timedelta(days=30),
                agent=False,
            )
        ]
    )

    result = reap_orphaned_agent_containers(
        runtime=runtime,
        active_names=set(),
        orphan_age_ms=_IMMEDIATE_REAP,
    )

    assert result == []
    assert runtime.removed == []


def test_runtime_list_timeout_does_not_break_orphan_reaper() -> None:
    class TimedOutRuntime(FakeRuntime):
        def list_containers(self, prefix: str = "pynchy-") -> list[FakeRuntimeContainer]:
            raise subprocess.TimeoutExpired(["container", "ls"], timeout=5)

    result = reap_orphaned_agent_containers(
        runtime=TimedOutRuntime([]),
        active_names=set(),
        orphan_age_ms=_IMMEDIATE_REAP,
    )

    assert result == []


def test_cleanup_runtime_images_prunes_dangling_images_only() -> None:
    runtime = FakeRuntime([])

    assert cleanup_runtime_images(runtime) is True

    assert runtime.pruned_images == [False]
