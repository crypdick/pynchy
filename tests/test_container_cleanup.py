"""Tests for orphaned agent container cleanup."""

from __future__ import annotations

import subprocess  # noqa: S404 - synthetic runtime timeout below.
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pynchy.identifiers import OrphanReapAgeMs
from pynchy.plugins.runtimes.cleanup import (
    ReapedContainer,
    cleanup_runtime_build_state,
    cleanup_runtime_builder,
    cleanup_runtime_images,
    reap_orphaned_agent_containers,
)

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


def test_cleanup_runtime_builder_is_optional_and_reports_failures() -> None:
    class NoBuilder:
        pass

    class SuccessfulBuilder:
        def cleanup_builder(self) -> bool:
            return True

    class FailedBuilder:
        def cleanup_builder(self) -> bool:
            return False

    class BrokenBuilder:
        def cleanup_builder(self) -> bool:
            raise OSError("builder unavailable")

    assert cleanup_runtime_builder(NoBuilder()) is True
    assert cleanup_runtime_builder(SuccessfulBuilder()) is True
    assert cleanup_runtime_builder(FailedBuilder()) is False
    assert cleanup_runtime_builder(BrokenBuilder()) is False


def test_cleanup_runtime_images_requires_a_successful_prune_operation() -> None:
    class NoPruner:
        pass

    class FailedPruner:
        def prune_images(self, *, all_images: bool = False) -> bool:
            assert all_images is False
            return False

    class BrokenPruner:
        def prune_images(self, *, all_images: bool = False) -> bool:
            raise subprocess.SubprocessError("prune failed")

    assert cleanup_runtime_images(NoPruner()) is False
    assert cleanup_runtime_build_state(FailedPruner()) is False
    assert cleanup_runtime_images(BrokenPruner()) is False


def test_reaper_skips_unknown_age_and_unrecognized_container_states() -> None:
    runtime = FakeRuntime(
        [
            FakeRuntimeContainer("pynchy-no-age", "running"),
            FakeRuntimeContainer(
                "pynchy-weird",
                "migrating",
                created_at=datetime.now(UTC) - timedelta(days=30),
            ),
            FakeRuntimeContainer(
                "pynchy-naive",
                "running",
                created_at=datetime(2020, 1, 1),  # noqa: DTZ001 - exercise naive timestamp normalization.
            ),
        ]
    )

    result = reap_orphaned_agent_containers(
        runtime=runtime,
        active_names=set(),
        orphan_age_ms=_SEVEN_DAYS,
    )

    assert result == [
        ReapedContainer("pynchy-naive", "running", "unowned-running"),
    ]


def test_reaper_continues_when_container_removal_fails() -> None:
    class RemovalRuntime(FakeRuntime):
        def __init__(self, *, error: bool) -> None:
            super().__init__(
                [
                    FakeRuntimeContainer(
                        "pynchy-stale",
                        "stopped",
                        created_at=datetime.now(UTC),
                    )
                ]
            )
            self.error = error

        def remove_container(self, name: str, *, force: bool = True) -> bool:
            if self.error:
                raise subprocess.SubprocessError("remove failed")
            return False

    assert (
        reap_orphaned_agent_containers(
            runtime=RemovalRuntime(error=False),
            active_names=set(),
            orphan_age_ms=_SEVEN_DAYS,
        )
        == []
    )
    assert (
        reap_orphaned_agent_containers(
            runtime=RemovalRuntime(error=True),
            active_names=set(),
            orphan_age_ms=_SEVEN_DAYS,
        )
        == []
    )
