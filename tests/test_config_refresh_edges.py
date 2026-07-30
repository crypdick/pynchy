"""Live configuration refresh boundary contracts."""

from __future__ import annotations

from asyncio import sleep
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from pynchy.host.orchestrator.api import (
    ConfigRefreshRuntime,
    ConfigRefreshStatus,
    configure_config_refresh_runtime,
    refresh_host_config,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class _NoPolicyChanges:
    affected_workspaces: tuple[str, ...] = ()
    live_changed: bool = False


async def test_refresh_rejects_missing_stable_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        "pynchy.host.orchestrator.config_refresh._load_stable_candidate",
        lambda _applied_hash: (None, None, None),
    )

    with pytest.raises(RuntimeError, match="Stable configuration candidate is missing"):
        await refresh_host_config("hash")


async def test_refresh_reports_unchanged_candidate(tmp_path: Path) -> None:
    candidate = object()
    runtime = ConfigRefreshRuntime(
        project_root=tmp_path,
        apply_candidate=_unexpected_apply,
        automation_projection=lambda _value: "automations",
        configuration_source_digest=lambda _root: "digest",
        get_settings=lambda: "published",
        load_runtime_candidate=lambda: candidate,
        restart_fingerprint=lambda _value: "hash",
        runtime_policy_changes=lambda _published, _candidate, _folders: _NoPolicyChanges(),
        workspace_folders=lambda: (),
    )
    configure_config_refresh_runtime(runtime)

    result = await refresh_host_config("hash")

    assert result.status is ConfigRefreshStatus.UNCHANGED
    assert result.restart_hash == "hash"


async def _unexpected_apply(
    _candidate: object,
    *,
    affected_workspaces: tuple[str, ...],
    reconcile_automations: bool,
) -> None:
    await sleep(0)
    raise AssertionError(f"unexpected apply: {affected_workspaces!r}, {reconcile_automations!r}")
