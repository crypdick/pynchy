"""Tests for bounded package-registry metadata and degraded policy."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.container_manager.ipc.handlers_artifact_security import (
    evaluate_package_coordinates,
)
from pynchy.host.container_manager.security.package_metadata import (
    PackageCoordinate,
    PackageEcosystem,
    PackageIntent,
    PackageMetadataAssessment,
    PackageMetadataState,
    PackageSource,
    RegistryMetadataError,
    assess_package_metadata,
    clear_package_metadata_cache,
)


def _coordinate(
    *,
    ecosystem: PackageEcosystem = PackageEcosystem.PYPI,
    name: str | None = "package-test-coordinate",
    version: str | None = "1.2.3",
    source: PackageSource = PackageSource.REGISTRY,
    intent: PackageIntent = PackageIntent.DEPENDENCY,
    lock_pinned: bool = False,
) -> PackageCoordinate:
    return PackageCoordinate(ecosystem, name, version, source, intent, lock_pinned)


def test_package_coordinate_boundary_rejects_extra_and_path_values() -> None:
    valid = {
        "ecosystem": "pypi",
        "name": "safe-name",
        "version": "1.2.3",
        "source": "registry",
        "intent": "dependency",
        "lock_pinned": False,
    }

    assert PackageCoordinate.from_wire(valid) is not None
    assert PackageCoordinate.from_wire({**valid, "command": "cat .env"}) is None
    assert PackageCoordinate.from_wire({**valid, "version": "../../workspace"}) is None
    assert PackageCoordinate.from_wire({**valid, "name": "../workspace"}) is None
    assert PackageCoordinate.from_wire({**valid, "lock_pinned": "false"}) is None
    assert PackageCoordinate.from_wire({**valid, "name": "a" * 215}) is None


@pytest.mark.asyncio
async def test_registry_age_classifies_fresh_and_established_releases() -> None:
    now = datetime(2026, 7, 19, tzinfo=UTC)

    async def fresh_fetcher(_coordinate: PackageCoordinate) -> datetime:
        await asyncio.sleep(0)
        return now - timedelta(days=2)

    async def established_fetcher(_coordinate: PackageCoordinate) -> datetime:
        await asyncio.sleep(0)
        return now - timedelta(days=30)

    clear_package_metadata_cache()
    fresh = await assess_package_metadata(
        _coordinate(name="package-fresh"),
        now=now,
        fetcher=fresh_fetcher,
    )
    established = await assess_package_metadata(
        _coordinate(name="package-established"),
        now=now,
        fetcher=established_fetcher,
    )

    assert fresh.state is PackageMetadataState.FRESH
    assert established.state is PackageMetadataState.ESTABLISHED


@pytest.mark.asyncio
async def test_registry_cache_is_content_addressed_by_coordinate() -> None:
    clear_package_metadata_cache()
    fetcher = AsyncMock(return_value=datetime(2026, 1, 1, tzinfo=UTC))
    coordinate = _coordinate(name="package-cache")

    await assess_package_metadata(coordinate, fetcher=fetcher)
    await assess_package_metadata(coordinate, fetcher=fetcher)

    fetcher.assert_awaited_once_with(coordinate)


@pytest.mark.asyncio
async def test_registry_failures_return_degraded_without_provider_details() -> None:
    async def failed_fetcher(_coordinate: PackageCoordinate) -> datetime:
        await asyncio.sleep(0)
        raise RegistryMetadataError("provider included a sensitive diagnostic")

    clear_package_metadata_cache()
    result = await assess_package_metadata(
        _coordinate(name="package-degraded"),
        fetcher=failed_fetcher,
    )

    assert result == PackageMetadataAssessment(
        PackageMetadataState.DEGRADED,
        "Authoritative package metadata is unavailable",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coordinate", "decision", "rule_id"),
    [
        (
            _coordinate(source=PackageSource.VCS),
            "needs_human",
            "PKG001",
        ),
        (
            _coordinate(source=PackageSource.CUSTOM_REGISTRY),
            "needs_human",
            "PKG001",
        ),
        (
            _coordinate(name=None, source=PackageSource.AMBIGUOUS),
            "deny",
            "PKG002",
        ),
        (
            _coordinate(version=None, intent=PackageIntent.EXECUTABLE),
            "needs_human",
            "PKG004",
        ),
    ],
)
async def test_static_package_policy(
    coordinate: PackageCoordinate,
    decision: str,
    rule_id: str,
) -> None:
    result, rule_ids = await evaluate_package_coordinates((coordinate,))

    assert result["decision"] == decision
    assert rule_id in rule_ids


@pytest.mark.asyncio
async def test_fresh_release_requires_approval() -> None:
    assessment = PackageMetadataAssessment(PackageMetadataState.FRESH, "fresh release")
    with patch(
        "pynchy.host.container_manager.ipc.handlers_artifact_security.assess_package_metadata",
        new_callable=AsyncMock,
        return_value=assessment,
    ):
        result, rule_ids = await evaluate_package_coordinates((_coordinate(),))

    assert result["decision"] == "needs_human"
    assert rule_ids == ("PKG006",)


@pytest.mark.asyncio
async def test_degraded_lock_reconciliation_continues_with_audit_rule() -> None:
    assessment = PackageMetadataAssessment(PackageMetadataState.DEGRADED, "unavailable")
    coordinate = _coordinate(intent=PackageIntent.RECONCILIATION, lock_pinned=True)
    with patch(
        "pynchy.host.container_manager.ipc.handlers_artifact_security.assess_package_metadata",
        new_callable=AsyncMock,
        return_value=assessment,
    ):
        result, rule_ids = await evaluate_package_coordinates((coordinate,))

    assert result["decision"] == "allow"
    assert rule_ids == ("PKG005",)


@pytest.mark.asyncio
async def test_degraded_executable_install_requires_approval() -> None:
    assessment = PackageMetadataAssessment(PackageMetadataState.DEGRADED, "unavailable")
    coordinate = _coordinate(intent=PackageIntent.EXECUTABLE)
    with patch(
        "pynchy.host.container_manager.ipc.handlers_artifact_security.assess_package_metadata",
        new_callable=AsyncMock,
        return_value=assessment,
    ):
        result, rule_ids = await evaluate_package_coordinates((coordinate,))

    assert result["decision"] == "needs_human"
    assert rule_ids == ("PKG005",)
