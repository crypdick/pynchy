"""Selective live refresh for validated host configuration."""

from __future__ import annotations

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, Protocol

from pynchy.logger import logger


class ConfigRefreshStatus(StrEnum):
    """Outcome of one host configuration refresh attempt."""

    UNCHANGED = "unchanged"
    REFRESHED = "config_refreshed"
    RUNTIME_POLICY_REFRESHED = "runtime_policy_refreshed"
    AUTOMATIONS_RECONCILED = "automations_reconciled"
    DEFERRED = "config_refresh_deferred"
    INVALID = "config_invalid"
    RESTART_REQUIRED = "restart_required"


@dataclass(frozen=True, slots=True)
class ConfigRefreshResult:
    """Configuration classification plus the candidate restart identity."""

    status: ConfigRefreshStatus
    restart_hash: str | None = None


class ApplyConfigCandidate(Protocol):
    """Publish one validated live candidate through the application owner."""

    def __call__(
        self,
        candidate: object,
        *,
        affected_workspaces: tuple[str, ...],
        reconcile_automations: bool,
    ) -> Awaitable[None]: ...


class RuntimePolicyChangeSet(Protocol):
    """Live policy classification returned by the configuration owner."""

    affected_workspaces: tuple[str, ...]
    live_changed: bool


@dataclass(frozen=True, slots=True)
class ConfigRefreshRuntime:
    """Configuration operations selected by the host composition root."""

    project_root: Path
    apply_candidate: ApplyConfigCandidate
    automation_projection: Callable[[object], object]
    configuration_source_digest: Callable[[Path], str]
    get_settings: Callable[[], object]
    load_runtime_candidate: Callable[[], object]
    restart_fingerprint: Callable[[object], str]
    runtime_policy_changes: Callable[
        [object, object, tuple[str, ...]],
        RuntimePolicyChangeSet,
    ]
    workspace_folders: Callable[[], tuple[str, ...]]


def _unconfigured_runtime(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("Host configuration refresh has not been composed")


_runtime = ConfigRefreshRuntime(
    project_root=Path(),
    apply_candidate=_unconfigured_runtime,
    automation_projection=_unconfigured_runtime,
    configuration_source_digest=_unconfigured_runtime,
    get_settings=_unconfigured_runtime,
    load_runtime_candidate=_unconfigured_runtime,
    restart_fingerprint=_unconfigured_runtime,
    runtime_policy_changes=_unconfigured_runtime,
    workspace_folders=_unconfigured_runtime,
)
_applied_automation_projection: object | None = None


def configure_config_refresh_runtime(runtime: ConfigRefreshRuntime) -> None:
    """Install the host's concrete configuration operations."""
    global _applied_automation_projection, _runtime  # noqa: PLW0603 - one host process owns configuration publication.
    _runtime = runtime
    _applied_automation_projection = runtime.automation_projection(runtime.get_settings())


def _load_stable_candidate(
    applied_restart_hash: str,
) -> tuple[ConfigRefreshResult | None, object | None, object | None]:
    try:
        source_before = _runtime.configuration_source_digest(_runtime.project_root)
        candidate = _runtime.load_runtime_candidate()
        candidate_restart_hash = _runtime.restart_fingerprint(candidate)
        candidate_automations = _runtime.automation_projection(candidate)
        source_after = _runtime.configuration_source_digest(_runtime.project_root)
    except (OSError, ValueError) as exc:
        logger.warning("Runtime configuration candidate is invalid", error=str(exc))
        return ConfigRefreshResult(ConfigRefreshStatus.INVALID), None, None

    if source_before != source_after:
        logger.info("Configuration changed while loading; deferring refresh")
        return ConfigRefreshResult(ConfigRefreshStatus.DEFERRED), None, None

    if candidate_restart_hash != applied_restart_hash:
        return (
            ConfigRefreshResult(
                ConfigRefreshStatus.RESTART_REQUIRED,
                candidate_restart_hash,
            ),
            None,
            None,
        )
    return None, candidate, candidate_automations


async def refresh_host_config(applied_restart_hash: str) -> ConfigRefreshResult:
    """Apply a stable live candidate or classify it for restart."""
    global _applied_automation_projection  # noqa: PLW0603 - updated only after publication succeeds.
    classified, candidate, candidate_automations = await asyncio.to_thread(
        _load_stable_candidate,
        applied_restart_hash,
    )
    if classified is not None:
        return classified
    if candidate is None or candidate_automations is None:
        raise RuntimeError("Stable configuration candidate is missing")
    published = _runtime.get_settings()
    policy_changes = _runtime.runtime_policy_changes(
        published,
        candidate,
        _runtime.workspace_folders(),
    )
    automations_changed = _applied_automation_projection != candidate_automations
    if not policy_changes.live_changed and not automations_changed:
        return ConfigRefreshResult(ConfigRefreshStatus.UNCHANGED, applied_restart_hash)

    await _runtime.apply_candidate(
        candidate,
        affected_workspaces=policy_changes.affected_workspaces,
        reconcile_automations=automations_changed,
    )
    _applied_automation_projection = candidate_automations
    if policy_changes.affected_workspaces:
        status = ConfigRefreshStatus.RUNTIME_POLICY_REFRESHED
    elif automations_changed:
        status = ConfigRefreshStatus.AUTOMATIONS_RECONCILED
    else:
        status = ConfigRefreshStatus.REFRESHED
    logger.info("Published updated live configuration", status=status.value)
    return ConfigRefreshResult(status, applied_restart_hash)
