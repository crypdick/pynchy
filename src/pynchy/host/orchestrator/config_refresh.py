"""Selective live refresh for validated host configuration."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves runtime operation annotations.
    Callable,
)
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from pynchy.logger import logger


class ConfigRefreshStatus(StrEnum):
    """Outcome of one host configuration refresh attempt."""

    UNCHANGED = "unchanged"
    REFRESHED = "config_refreshed"
    DEFERRED = "config_refresh_deferred"
    INVALID = "config_invalid"
    RESTART_REQUIRED = "restart_required"


@dataclass(frozen=True, slots=True)
class ConfigRefreshResult:
    """Configuration classification plus the candidate restart identity."""

    status: ConfigRefreshStatus
    restart_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigRefreshRuntime:
    """Configuration operations selected by the host composition root."""

    project_root: Path
    configuration_source_digest: Callable[[Path], str]
    get_settings: Callable[[], object]
    load_runtime_candidate: Callable[[], object]
    publish_settings: Callable[[object], None]
    restart_fingerprint: Callable[[object], str]
    skill_policy_projection: Callable[[object], object]


def _unconfigured_runtime(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("Host configuration refresh has not been composed")


_runtime = ConfigRefreshRuntime(
    project_root=Path(),
    configuration_source_digest=_unconfigured_runtime,
    get_settings=_unconfigured_runtime,
    load_runtime_candidate=_unconfigured_runtime,
    publish_settings=_unconfigured_runtime,
    restart_fingerprint=_unconfigured_runtime,
    skill_policy_projection=_unconfigured_runtime,
)


def configure_config_refresh_runtime(runtime: ConfigRefreshRuntime) -> None:
    """Install the host's concrete configuration operations."""
    global _runtime  # noqa: PLW0603 - one host process owns configuration publication.
    _runtime = runtime


def refresh_host_config(applied_restart_hash: str) -> ConfigRefreshResult:
    """Publish a stable skill-only candidate or classify it for restart."""
    try:
        source_before = _runtime.configuration_source_digest(_runtime.project_root)
        candidate = _runtime.load_runtime_candidate()
        candidate_restart_hash = _runtime.restart_fingerprint(candidate)
        source_after = _runtime.configuration_source_digest(_runtime.project_root)
    except (OSError, ValueError) as exc:
        logger.warning("Runtime configuration candidate is invalid", error=str(exc))
        return ConfigRefreshResult(ConfigRefreshStatus.INVALID)

    if source_before != source_after:
        logger.info("Configuration changed while loading; deferring refresh")
        return ConfigRefreshResult(ConfigRefreshStatus.DEFERRED)

    if candidate_restart_hash != applied_restart_hash:
        return ConfigRefreshResult(
            ConfigRefreshStatus.RESTART_REQUIRED,
            candidate_restart_hash,
        )

    published = _runtime.get_settings()
    if _runtime.skill_policy_projection(published) == _runtime.skill_policy_projection(candidate):
        return ConfigRefreshResult(ConfigRefreshStatus.UNCHANGED, candidate_restart_hash)

    _runtime.publish_settings(candidate)
    logger.info("Published updated workspace skill policy")
    return ConfigRefreshResult(ConfigRefreshStatus.REFRESHED, candidate_restart_hash)
