"""System checks and setup for external dependencies (Tailscale, containers)."""

from __future__ import annotations

import contextlib
import json
import subprocess

from pynchy.config import CONTAINER_IMAGE, PROJECT_ROOT
from pynchy.logger import logger
from pynchy.runtime import get_runtime


def check_tailscale() -> None:
    """Log a warning if Tailscale is not connected. Non-fatal."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.warning("Tailscale not connected (non-fatal)", stderr=result.stderr.strip())
            return
        status = json.loads(result.stdout)
        backend = status.get("BackendState", "")
        if backend != "Running":
            logger.warning("Tailscale backend not running", state=backend)
        else:
            logger.info("Tailscale connected", state=backend)
    except FileNotFoundError:
        logger.warning("Tailscale CLI not found (non-fatal)")
    except Exception as exc:
        logger.warning("Tailscale check failed (non-fatal)", err=str(exc))


def ensure_container_system_running() -> None:
    """Verify container runtime is available and stop orphaned containers."""
    runtime = get_runtime()
    runtime.ensure_running()

    # Auto-build container image if missing
    result = subprocess.run(
        [runtime.cli, "image", "inspect", CONTAINER_IMAGE],
        capture_output=True,
    )
    if result.returncode != 0:
        container_dir = PROJECT_ROOT / "container"
        if not (container_dir / "Dockerfile").exists():
            raise RuntimeError(
                f"Container image '{CONTAINER_IMAGE}' not found and "
                f"no Dockerfile at {container_dir / 'Dockerfile'}"
            )
        logger.info("Container image not found, building...", image=CONTAINER_IMAGE)
        build = subprocess.run(
            [runtime.cli, "build", "-t", CONTAINER_IMAGE, "."],
            cwd=str(container_dir),
        )
        if build.returncode != 0:
            raise RuntimeError(f"Failed to build container image '{CONTAINER_IMAGE}'")

    # Kill orphaned containers from previous runs
    orphans = runtime.list_running_containers("pynchy-")
    for name in orphans:
        with contextlib.suppress(Exception):
            subprocess.run(
                [runtime.cli, "stop", name],
                capture_output=True,
            )
    if orphans:
        logger.info(
            "Stopped orphaned containers",
            count=len(orphans),
            names=orphans,
        )
