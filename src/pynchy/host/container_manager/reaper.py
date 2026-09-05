"""Reap runtime-test container resources abandoned by earlier runs."""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404 - fixed docker CLI argv, no shell.
import time
from collections.abc import Callable, Container, Iterable, Mapping
from datetime import datetime
from pathlib import Path

from pynchy.container_labels import (
    NAMESPACE_LABEL,
    OWNER_BOOT_LABEL,
    OWNER_PID_LABEL,
    PROVENANCE_LABEL,
    PROVENANCE_LABEL_VALUE_TEST,
)
from pynchy.runtime_names import PRODUCTION_NAMESPACE, runtime_namespace

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

# Hosts without /proc still need a marker that is stable within a boot but
# differs across boots. Process 1's start time satisfies both.
_INIT_PID = 1

# Long enough that no runtime test legitimately outlives it, and only consulted
# when ownership cannot be verified at all.
UNOWNED_STALE_AGE_SECONDS = 30.0 * 60.0


def provenance_label_args(*, namespace: str, pid: int, boot_id: str) -> list[str]:
    """Return ``docker`` flags stamping test provenance and ownership.

    Every resource the harness creates carries these so the reaper can judge it
    from the resource alone, without needing harness state to still exist.
    """
    labels = {
        PROVENANCE_LABEL: PROVENANCE_LABEL_VALUE_TEST,
        NAMESPACE_LABEL: namespace,
        OWNER_PID_LABEL: str(pid),
        OWNER_BOOT_LABEL: boot_id,
    }
    args: list[str] = []
    for key, value in labels.items():
        args.extend(("--label", f"{key}={value}"))
    return args


def runtime_provenance_label_args() -> list[str]:
    """Return provenance labels when this process runs in a sandbox namespace.

    Production runs in the default namespace and gets no labels at all, which
    keeps its containers outside the reaper's reach.
    """
    namespace = runtime_namespace()
    if namespace == PRODUCTION_NAMESPACE:
        return []
    return provenance_label_args(namespace=namespace, pid=os.getpid(), boot_id=current_boot_id())


def runtime_restart_policy_args() -> list[str]:
    """Return the restart policy flags appropriate to this namespace.

    Sandbox containers must not outlive their run: a restarting policy revives
    abandoned sandbox containers at every boot, which is how they accumulate.
    """
    if runtime_namespace() == PRODUCTION_NAMESPACE:
        return ["--restart", "unless-stopped"]
    return ["--restart", "no"]


def is_reapable(
    labels: Mapping[str, str],
    *,
    age_seconds: float,
    live_pids: Container[int],
    boot_id: str,
) -> bool:
    """Return whether a labelled resource is safe to reap.

    Only resources this harness labelled as test provenance are candidates, so
    production resources are never reachable regardless of age or naming.
    """
    if labels.get(PROVENANCE_LABEL) != PROVENANCE_LABEL_VALUE_TEST:
        return False
    owner_boot = labels.get(OWNER_BOOT_LABEL)
    if owner_boot is not None and owner_boot != boot_id:
        # The owner cannot still be running: the host rebooted since creation.
        return True
    owner_pid = _parse_pid(labels.get(OWNER_PID_LABEL))
    if owner_pid is None or owner_boot is None:
        return age_seconds > UNOWNED_STALE_AGE_SECONDS
    return owner_pid not in live_pids


def _parse_pid(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit():
        return None
    pid = int(raw)
    return pid if pid > 0 else None


def current_boot_id() -> str:
    """Return an identifier that changes when the host reboots."""
    try:
        return BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return f"init-{_init_start_time()}"


def _init_start_time() -> str:
    try:
        return str(int(Path(f"/proc/{_INIT_PID}").stat().st_ctime))
    except OSError:
        return "unknown"


def live_pids() -> frozenset[int]:
    """Return the PIDs currently running on this host."""
    try:
        return frozenset(
            int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()
        )
    except OSError:
        return frozenset({os.getpid()})


# Markers whose tests provision real Docker resources.
DOCKER_MARKERS = frozenset({"runtime", "live"})

DockerRun = Callable[[list[str]], str]


def wants_reaping(marker_names: Iterable[str]) -> bool:  # noqa: V103
    """Return whether a collected session provisions Docker resources."""
    return any(name in DOCKER_MARKERS for name in marker_names)


def reap_orphaned_test_resources(
    *,
    run: DockerRun,
    live_pids: Container[int],
    boot_id: str,
    now: float,
) -> list[str]:
    """Reap test containers whose owning run is gone, returning their names.

    The provenance filter is applied by Docker itself as well as by
    ``is_reapable``, so production resources are excluded twice over.
    """
    listed = run(
        ["ps", "-aq", "--filter", f"label={PROVENANCE_LABEL}={PROVENANCE_LABEL_VALUE_TEST}"]
    )
    names = [line.strip() for line in listed.splitlines() if line.strip()]
    doomed = [
        name
        for name in names
        if _is_reapable_resource(run, name, live_pids=live_pids, boot_id=boot_id, now=now)
    ]
    if doomed:
        run(["rm", "-f", *doomed])
    return doomed


def _is_reapable_resource(
    run: DockerRun,
    name: str,
    *,
    live_pids: Container[int],
    boot_id: str,
    now: float,
) -> bool:
    inspected = json.loads(run(["inspect", "--format", "{{json .}}", name]))
    labels = inspected.get("Labels") or {}
    created = _created_epoch(inspected.get("Created"))
    return is_reapable(
        labels,
        age_seconds=max(now - created, 0.0),
        live_pids=live_pids,
        boot_id=boot_id,
    )


def _created_epoch(created: object) -> float:
    """Convert a docker ``Created`` value to epoch seconds.

    ``docker inspect`` reports RFC3339 text with nanosecond precision, which
    ``datetime.fromisoformat`` rejects before Python 3.11 semantics settle it, so
    the fractional part is trimmed to microseconds first.
    """
    if isinstance(created, int | float):
        return float(created)
    if not isinstance(created, str) or not created:
        return 0.0
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", created.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(trimmed).timestamp()
    except ValueError:
        return 0.0


def default_docker_run(cli: str = "docker") -> DockerRun:
    """Return a runner executing the real docker CLI."""

    def run(args: list[str]) -> str:
        completed = subprocess.run(  # noqa: S603 - fixed argv built from constants and resource names.
            [cli, *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        return completed.stdout

    return run


def reap_now(cli: str = "docker") -> list[str]:  # noqa: V103
    """Sweep abandoned test resources using real host state."""
    return reap_orphaned_test_resources(
        run=default_docker_run(cli),
        live_pids=live_pids(),
        boot_id=current_boot_id(),
        now=time.time(),
    )
