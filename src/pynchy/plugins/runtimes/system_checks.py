"""System checks and setup for external dependencies (containers)."""

from __future__ import annotations

import contextlib
import subprocess

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.plugins.runtimes.detection import get_runtime


def ensure_container_system_running() -> None:
    """Verify container runtime is available and stop orphaned containers."""
    runtime = get_runtime()
    runtime.ensure_running()

    # Auto-build container image if missing
    s = get_settings()
    image = s.container.image
    result = subprocess.run(
        [runtime.cli, "image", "inspect", image],
        capture_output=True,
    )
    if result.returncode != 0:
        container_dir = s.project_root / "src" / "pynchy" / "agent"
        if not (container_dir / "Dockerfile").exists():
            raise RuntimeError(
                f"Container image '{image}' not found and "
                f"no Dockerfile at {container_dir / 'Dockerfile'}"
            )
        logger.info("Container image not found, building...", image=image)
        build = subprocess.run(
            [runtime.cli, "build", "-t", image, "."],
            cwd=str(container_dir),
        )
        if build.returncode != 0:
            raise RuntimeError(f"Failed to build container image '{image}'")

    # Kill orphaned containers from previous runs
    orphans = runtime.list_running_containers("pynchy-")
    for name in orphans:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
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
