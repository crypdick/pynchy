"""Behavior contracts for reaping orphaned runtime-test container resources."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from pynchy.container_labels import (
    AGENT_CONTAINER_LABEL,
    AGENT_CONTAINER_LABEL_VALUE,
    NAMESPACE_LABEL,
    OWNER_BOOT_LABEL,
    OWNER_PID_LABEL,
    PROVENANCE_LABEL,
    PROVENANCE_LABEL_VALUE_TEST,
)
from pynchy.host.container_manager import reaper

_BOOT = "boot-abc"
_DAY_SECONDS = 60.0 * 60.0 * 24.0
_FRESH_SECONDS = 60.0


def _test_labels(*, pid: int = 4242, boot: str = _BOOT) -> dict[str, str]:
    return {
        PROVENANCE_LABEL: PROVENANCE_LABEL_VALUE_TEST,
        OWNER_PID_LABEL: str(pid),
        OWNER_BOOT_LABEL: boot,
    }


def _is_reapable(labels: dict[str, str], *, age_seconds: float, live: set[int]) -> bool:
    return reaper.is_reapable(labels, age_seconds=age_seconds, live_pids=live, boot_id=_BOOT)


def test_resource_owned_by_a_live_process_is_never_reaped() -> None:
    """A sibling suite still running must survive regardless of age."""
    reapable = _is_reapable(_test_labels(pid=4242), age_seconds=_DAY_SECONDS, live={4242})

    assert reapable is False


def test_resource_owned_by_a_dead_process_is_reaped_immediately() -> None:
    """An abandoned resource needs no waiting period once its owner is gone."""
    reapable = _is_reapable(_test_labels(pid=4242), age_seconds=_FRESH_SECONDS, live=set())

    assert reapable is True


def test_resource_from_an_earlier_boot_is_reaped_even_if_its_pid_is_reused() -> None:
    """PIDs are recycled across boots, so the boot marker decides."""
    labels = _test_labels(pid=4242, boot="boot-previous")

    reapable = _is_reapable(labels, age_seconds=_FRESH_SECONDS, live={4242})

    assert reapable is True


def test_unverifiable_owner_is_kept_until_the_stale_age() -> None:
    """Without ownership the reaper must not race a run that just started."""
    labels = {PROVENANCE_LABEL: PROVENANCE_LABEL_VALUE_TEST}

    reapable = _is_reapable(labels, age_seconds=_FRESH_SECONDS, live=set())

    assert reapable is False


def test_unverifiable_owner_is_reaped_once_stale() -> None:
    labels = {PROVENANCE_LABEL: PROVENANCE_LABEL_VALUE_TEST}

    reapable = _is_reapable(labels, age_seconds=_DAY_SECONDS, live=set())

    assert reapable is True


def test_unlabelled_resource_is_never_reaped() -> None:
    """Legacy orphans are deliberately out of reach of the reaper."""
    reapable = _is_reapable({}, age_seconds=_DAY_SECONDS, live=set())

    assert reapable is False


def test_production_agent_resource_is_never_reaped() -> None:
    """The safety property: production containers are unreachable by design."""
    labels = {AGENT_CONTAINER_LABEL: AGENT_CONTAINER_LABEL_VALUE}

    reapable = _is_reapable(labels, age_seconds=_DAY_SECONDS, live=set())

    assert reapable is False


def test_provenance_label_args_stamp_every_field_the_reaper_reads() -> None:
    """Resources must carry enough provenance to be judged without harness state."""
    args = reaper.provenance_label_args(namespace="pynchy-runtime-x", pid=99, boot_id=_BOOT)

    assert args == [
        "--label",
        f"{PROVENANCE_LABEL}={PROVENANCE_LABEL_VALUE_TEST}",
        "--label",
        f"{NAMESPACE_LABEL}=pynchy-runtime-x",
        "--label",
        f"{OWNER_PID_LABEL}=99",
        "--label",
        f"{OWNER_BOOT_LABEL}={_BOOT}",
    ]


def test_current_boot_id_reads_the_kernel_boot_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot_file = tmp_path / "boot_id"
    boot_file.write_text("f00dfeed\n")
    monkeypatch.setattr(reaper, "BOOT_ID_PATH", boot_file)

    assert reaper.current_boot_id() == "f00dfeed"


def test_current_boot_id_falls_back_when_the_kernel_file_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosts without /proc (macOS) still need a usable marker."""
    monkeypatch.setattr(reaper, "BOOT_ID_PATH", tmp_path / "missing")

    boot_id = reaper.current_boot_id()

    assert boot_id
    assert boot_id == reaper.current_boot_id()


def test_live_pids_includes_the_running_process() -> None:
    assert os.getpid() in reaper.live_pids()


def _fake_docker(containers: dict[str, dict[str, object]], removed: list[str]):
    """Stand in for the docker CLI over the inspect/remove surface the reaper uses."""

    def run(args: list[str]) -> str:
        if args[:2] == ["ps", "-aq"]:
            return "\n".join(containers)
        if args[0] == "inspect":
            name = args[-1]
            return json.dumps(containers[name])
        if args[:2] == ["rm", "-f"]:
            removed.extend(args[2:])
            return ""
        raise AssertionError(f"unexpected docker call: {args}")

    return run


def test_sweep_removes_abandoned_resources_and_spares_live_ones() -> None:
    """End-to-end selection: only the dead owner's container is removed."""
    containers = {
        "orphan": {"Labels": _test_labels(pid=999999), "Created": 0.0},
        "sibling": {"Labels": _test_labels(pid=os.getpid()), "Created": 0.0},
        "production": {
            "Labels": {AGENT_CONTAINER_LABEL: AGENT_CONTAINER_LABEL_VALUE},
            "Created": 0.0,
        },
    }
    removed: list[str] = []

    reaped = reaper.reap_orphaned_test_resources(
        run=_fake_docker(containers, removed),
        live_pids=frozenset({os.getpid()}),
        boot_id=_BOOT,
        now=0.0,
    )

    assert reaped == ["orphan"]
    assert removed == ["orphan"]


def test_sweep_parses_the_iso_timestamp_docker_actually_returns() -> None:
    """docker inspect reports Created as RFC3339 text, not an epoch number."""
    containers = {
        "stale-unowned": {
            "Labels": {PROVENANCE_LABEL: PROVENANCE_LABEL_VALUE_TEST},
            "Created": "2026-08-18T00:00:00.123456789Z",
        },
    }
    removed: list[str] = []
    a_day_later = datetime(2026, 8, 19, tzinfo=UTC).timestamp()

    reaped = reaper.reap_orphaned_test_resources(
        run=_fake_docker(containers, removed),
        live_pids=frozenset(),
        boot_id=_BOOT,
        now=a_day_later,
    )

    assert reaped == ["stale-unowned"]


def test_only_docker_using_sessions_want_reaping() -> None:
    """A default unit run must not pay any docker cost."""
    assert reaper.wants_reaping(["action", "parity"]) is False
    assert reaper.wants_reaping(["runtime"]) is True
    assert reaper.wants_reaping(["live"]) is True


def test_sandbox_runtime_stamps_provenance_but_production_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production containers must stay unlabelled so the reaper cannot see them."""
    monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy")
    assert reaper.runtime_provenance_label_args() == []

    monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy-runtime-abc123")
    args = reaper.runtime_provenance_label_args()

    assert f"{PROVENANCE_LABEL}={PROVENANCE_LABEL_VALUE_TEST}" in args
    assert f"{NAMESPACE_LABEL}=pynchy-runtime-abc123" in args


def test_sandbox_containers_do_not_survive_a_reboot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`unless-stopped` is what resurrected abandoned sandbox containers at boot."""
    monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy")
    assert reaper.runtime_restart_policy_args() == ["--restart", "unless-stopped"]

    monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy-runtime-abc123")
    assert reaper.runtime_restart_policy_args() == ["--restart", "no"]
