"""Public package-metadata API contracts against bounded registry responses."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self
from unittest.mock import patch

import pytest

from pynchy.host.container_manager.api import (
    PackageCoordinate,
    PackageEcosystem,
    PackageIntent,
    PackageMetadataState,
    PackageSource,
    assess_package_metadata,
    clear_package_metadata_cache,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Content:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        yield self._payload


class _Response:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self.content = _Content(payload)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.request: tuple[str, dict[str, str]] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str]) -> _Response:
        self.request = (url, headers)
        return self.response


def _coordinate(
    ecosystem: PackageEcosystem = PackageEcosystem.PYPI,
    *,
    name: str = "package-test-coordinate",
    version: str = "1.2.3",
    source: PackageSource = PackageSource.REGISTRY,
) -> PackageCoordinate:
    return PackageCoordinate(
        ecosystem,
        name,
        version,
        source,
        PackageIntent.DEPENDENCY,
        False,
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"ecosystem": "pypi"},
        {
            "ecosystem": "unknown",
            "name": "safe-name",
            "version": "1.2.3",
            "source": "registry",
            "intent": "dependency",
            "lock_pinned": False,
        },
        {
            "ecosystem": "pypi",
            "name": 3,
            "version": "1.2.3",
            "source": "registry",
            "intent": "dependency",
            "lock_pinned": False,
        },
    ],
)
def test_package_coordinate_api_rejects_noncanonical_wire_values(value: object) -> None:
    assert PackageCoordinate.from_wire(value) is None


@pytest.mark.asyncio
async def test_package_metadata_api_degrades_without_an_exact_registry_reference() -> None:
    result = await assess_package_metadata(
        _coordinate(source=PackageSource.DIRECT_URL),
    )

    assert result.state is PackageMetadataState.DEGRADED
    assert result.reason == "No exact authoritative registry coordinate is available"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coordinate", "payload", "url"),
    [
        (
            _coordinate(),
            {"urls": [{"upload_time_iso_8601": "2026-01-01T00:00:00+00:00"}]},
            "https://pypi.org/pypi/package-test-coordinate/1.2.3/json",
        ),
        (
            _coordinate(PackageEcosystem.NPM, name="@scope/package"),
            {"time": {"1.2.3": "2026-01-01T00:00:00+00:00"}},
            "https://registry.npmjs.org/@scope/package",
        ),
        (
            _coordinate(PackageEcosystem.CARGO, name="package_test"),
            {"version": {"created_at": "2026-01-01T00:00:00+00:00"}},
            "https://crates.io/api/v1/crates/package_test/1.2.3",
        ),
    ],
)
async def test_package_metadata_api_reads_each_authoritative_registry(
    coordinate: PackageCoordinate,
    payload: dict[str, object],
    url: str,
) -> None:
    session = _Session(_Response(200, json.dumps(payload).encode()))
    clear_package_metadata_cache()

    with patch(
        "pynchy.host.container_manager.security.package_metadata.aiohttp.ClientSession",
        return_value=session,
    ):
        result = await assess_package_metadata(
            coordinate,
            now=datetime(2026, 2, 1, tzinfo=UTC),
        )

    assert result.state is PackageMetadataState.ESTABLISHED
    assert session.request == (url, {"Accept": "application/json"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (404, b"{}"),
        (200, b"not json"),
        (200, b"[]"),
        (200, json.dumps({"urls": []}).encode()),
        (200, json.dumps({"urls": [{"upload_time_iso_8601": "2026-01-01"}]}).encode()),
        (200, json.dumps({"time": {}}).encode()),
        (200, json.dumps({"version": {}}).encode()),
        (200, b"x" * (256 * 1024 + 1)),
    ],
    ids=(
        "status",
        "not-json",
        "array",
        "missing-artifacts",
        "naive-time",
        "missing-npm-time",
        "missing-cargo-time",
        "oversized",
    ),
)
async def test_package_metadata_api_degrades_for_invalid_registry_responses(
    status: int,
    payload: bytes,
) -> None:
    clear_package_metadata_cache()
    session = _Session(_Response(status, payload))

    with patch(
        "pynchy.host.container_manager.security.package_metadata.aiohttp.ClientSession",
        return_value=session,
    ):
        result = await assess_package_metadata(_coordinate())

    assert result.state is PackageMetadataState.DEGRADED
    assert result.reason == "Authoritative package metadata is unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ecosystem",
    [PackageEcosystem.NPM, PackageEcosystem.CARGO],
)
async def test_package_metadata_api_degrades_when_release_timestamp_is_missing(
    ecosystem: PackageEcosystem,
) -> None:
    payload = {"time": {}} if ecosystem is PackageEcosystem.NPM else {"version": {}}
    session = _Session(_Response(200, json.dumps(payload).encode()))
    clear_package_metadata_cache()

    with patch(
        "pynchy.host.container_manager.security.package_metadata.aiohttp.ClientSession",
        return_value=session,
    ):
        result = await assess_package_metadata(_coordinate(ecosystem=ecosystem))

    assert result.state is PackageMetadataState.DEGRADED


@pytest.mark.asyncio
async def test_package_metadata_api_treats_seven_day_boundary_as_established() -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)

    async def fetcher(_coordinate: PackageCoordinate) -> datetime:
        await asyncio.sleep(0)
        return now - timedelta(days=7)

    clear_package_metadata_cache()
    result = await assess_package_metadata(_coordinate(), now=now, fetcher=fetcher)

    assert result.state is PackageMetadataState.ESTABLISHED
