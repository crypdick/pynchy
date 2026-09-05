"""Bounded authoritative-registry metadata checks for package operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import quote

import aiohttp

_REQUEST_TIMEOUT_SECONDS = 3
_MAX_RESPONSE_BYTES = 256 * 1024
_CACHE_TTL_SECONDS = 6 * 60 * 60
_FRESH_RELEASE_WINDOW = timedelta(days=7)
_COORDINATE_KEYS = frozenset({"ecosystem", "name", "version", "source", "intent", "lock_pinned"})
_PYPI_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NPM_NAME = re.compile(r"^(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+$")
_CARGO_NAME = re.compile(r"^[a-z0-9_-]+$")


@runtime_checkable
class _BoundedResponseContent(Protocol):
    def iter_chunked(self, size: int) -> AsyncIterator[bytes]: ...


@runtime_checkable  # noqa: V102
class _BoundedResponse(Protocol):
    status: int
    content: _BoundedResponseContent


class PackageEcosystem(StrEnum):
    """Supported authoritative package registries."""

    PYPI = "pypi"
    NPM = "npm"
    CARGO = "cargo"


class PackageSource(StrEnum):
    """Normalized source class supplied by the agent boundary."""

    REGISTRY = "registry"
    DIRECT_URL = "direct_url"
    VCS = "vcs"
    LOCAL = "local"
    SHELL = "shell"
    AMBIGUOUS = "ambiguous"
    CUSTOM_REGISTRY = "custom_registry"


class PackageIntent(StrEnum):
    """Operation that caused the package reference."""

    DEPENDENCY = "dependency"
    EXECUTABLE = "executable"
    RECONCILIATION = "reconciliation"


class PackageMetadataState(StrEnum):
    """Age/availability result from an authoritative registry."""

    ESTABLISHED = "established"
    FRESH = "fresh"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class PackageCoordinate:
    """Strict normalized package coordinate accepted from an agent."""

    ecosystem: PackageEcosystem
    name: str | None
    version: str | None
    source: PackageSource
    intent: PackageIntent
    lock_pinned: bool

    @classmethod
    def from_wire(cls, value: object) -> PackageCoordinate | None:
        """Parse a coordinate only when its schema and normalized values are exact."""
        if not isinstance(value, dict) or set(value) != _COORDINATE_KEYS:
            return None
        name = value.get("name")
        version = value.get("version")
        lock_pinned = value.get("lock_pinned")
        ecosystem = value.get("ecosystem")
        source = value.get("source")
        intent = value.get("intent")
        if (name is not None and not isinstance(name, str)) or (
            version is not None and not isinstance(version, str)
        ):
            return None
        if (
            not isinstance(lock_pinned, bool)
            or not isinstance(ecosystem, str)
            or not isinstance(source, str)
            or not isinstance(intent, str)
        ):
            return None
        try:
            coordinate = cls(
                ecosystem=PackageEcosystem(ecosystem),
                name=name,
                version=version,
                source=PackageSource(source),
                intent=PackageIntent(intent),
                lock_pinned=lock_pinned,
            )
        except (TypeError, ValueError):
            return None
        return coordinate if coordinate.is_normalized else None

    @property
    def is_normalized(self) -> bool:
        """Reject path/query/control data at the registry boundary."""
        if self.name is not None and not _valid_name(self.ecosystem, self.name):
            return False
        return not (
            self.version is not None
            and (
                len(self.version) > 128
                or any(character.isspace() or character in "/?#\\" for character in self.version)
            )
        )

    @property
    def cache_key(self) -> str:
        """Return a content address for the normalized registry coordinate."""
        canonical = json.dumps(
            [self.ecosystem.value, self.name, self.version],
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class PackageMetadataAssessment:
    """Policy-facing registry result without raw provider payloads."""

    state: PackageMetadataState
    reason: str


@dataclass(frozen=True)
class _CacheEntry:
    released_at: datetime
    expires_at: float


type RegistryFetcher = Callable[[PackageCoordinate], Awaitable[datetime]]

_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = asyncio.Lock()


async def assess_package_metadata(
    coordinate: PackageCoordinate,
    *,
    now: datetime | None = None,
    fetcher: RegistryFetcher | None = None,
) -> PackageMetadataAssessment:
    """Classify exact registry coordinates by authoritative release age."""
    if (
        coordinate.source is not PackageSource.REGISTRY
        or coordinate.name is None
        or coordinate.version is None
    ):
        return PackageMetadataAssessment(
            PackageMetadataState.DEGRADED,
            "No exact authoritative registry coordinate is available",
        )
    try:
        released_at = await _cached_release_time(
            coordinate,
            fetcher=fetcher or _fetch_registry_release_time,
        )
    except (RegistryMetadataError, aiohttp.ClientError, TimeoutError, ValueError):
        return PackageMetadataAssessment(
            PackageMetadataState.DEGRADED,
            "Authoritative package metadata is unavailable",
        )
    current = now or datetime.now(UTC)
    if current - released_at < _FRESH_RELEASE_WINDOW:
        return PackageMetadataAssessment(
            PackageMetadataState.FRESH,
            "Package release is inside the seven-day freshness window",
        )
    return PackageMetadataAssessment(
        PackageMetadataState.ESTABLISHED,
        "Package release predates the freshness window",
    )


class RegistryMetadataError(RuntimeError):
    """The authoritative registry did not return bounded valid metadata."""


async def _cached_release_time(
    coordinate: PackageCoordinate,
    *,
    fetcher: RegistryFetcher,
) -> datetime:
    key = coordinate.cache_key
    current = monotonic()
    async with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached.expires_at > current:
            return cached.released_at
    released_at = await fetcher(coordinate)
    async with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(
            released_at=released_at,
            expires_at=monotonic() + _CACHE_TTL_SECONDS,
        )
    return released_at


async def _fetch_registry_release_time(coordinate: PackageCoordinate) -> datetime:
    url = _registry_url(coordinate)
    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
    async with (
        aiohttp.ClientSession(timeout=timeout, raise_for_status=False) as session,
        session.get(url, headers={"Accept": "application/json"}) as response,
    ):
        if response.status != 200:
            raise RegistryMetadataError(f"Registry returned status {response.status}")
        payload = await _bounded_json(response)
    return _release_time(coordinate, payload)


async def _bounded_json(response: object) -> dict[str, Any]:
    bounded_response = cast("_BoundedResponse", response)
    content = bytearray()
    async for chunk in bounded_response.content.iter_chunked(16 * 1024):
        content.extend(chunk)
        if len(content) > _MAX_RESPONSE_BYTES:
            raise RegistryMetadataError("Registry response exceeded the size limit")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryMetadataError("Registry response was not JSON") from exc
    if not isinstance(payload, dict):
        raise RegistryMetadataError("Registry response did not match the expected schema")
    return payload


def _registry_url(coordinate: PackageCoordinate) -> str:
    name = quote(cast("str", coordinate.name), safe="@/")
    version = quote(cast("str", coordinate.version), safe="")
    if coordinate.ecosystem is PackageEcosystem.PYPI:
        return f"https://pypi.org/pypi/{name}/{version}/json"
    if coordinate.ecosystem is PackageEcosystem.NPM:
        return f"https://registry.npmjs.org/{name}"
    return f"https://crates.io/api/v1/crates/{name}/{version}"


def _release_time(coordinate: PackageCoordinate, payload: dict[str, Any]) -> datetime:
    timestamp: object
    if coordinate.ecosystem is PackageEcosystem.PYPI:
        urls = payload.get("urls")
        if not isinstance(urls, list) or not urls:
            raise RegistryMetadataError("PyPI release has no artifacts")
        timestamps = [item.get("upload_time_iso_8601") for item in urls if isinstance(item, dict)]
        timestamp = next((item for item in timestamps if isinstance(item, str)), None)
    elif coordinate.ecosystem is PackageEcosystem.NPM:
        times = payload.get("time")
        timestamp = times.get(coordinate.version) if isinstance(times, dict) else None
    else:
        version = payload.get("version")
        timestamp = version.get("created_at") if isinstance(version, dict) else None
    if not isinstance(timestamp, str):
        raise RegistryMetadataError("Registry release timestamp is missing")
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        raise RegistryMetadataError("Registry release timestamp has no timezone")
    return parsed.astimezone(UTC)


def _valid_name(ecosystem: PackageEcosystem, name: str) -> bool:
    if len(name) > 214:
        return False
    pattern = {
        PackageEcosystem.PYPI: _PYPI_NAME,
        PackageEcosystem.NPM: _NPM_NAME,
        PackageEcosystem.CARGO: _CARGO_NAME,
    }[ecosystem]
    return pattern.fullmatch(name) is not None


def clear_package_metadata_cache() -> None:
    """Clear the process cache for deterministic tests and operator refreshes."""
    _CACHE.clear()
