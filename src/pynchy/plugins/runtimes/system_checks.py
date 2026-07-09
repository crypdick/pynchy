"""System checks and setup for external dependencies (containers)."""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - system checks use fixed no-shell runtime CLI argv.

from pynchy.config import get_settings
from pynchy.host.container_manager.cleanup import (
    cleanup_runtime_builder,
    cleanup_runtime_images,
    reap_orphaned_agent_containers,
)
from pynchy.logger import logger
from pynchy.plugins.runtimes.detection import get_runtime


def ensure_container_system_running() -> None:
    """Verify container runtime is available and reap orphaned agent containers."""
    runtime = get_runtime()
    runtime.ensure_running()

    # Auto-build container image if missing
    s = get_settings()
    image = s.container.image
    result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is selected by trusted runtime detection and argv is fixed.
        [runtime.cli, "image", "inspect", image],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        container_dir = s.project_root / "src" / "pynchy" / "agent"
        if not (container_dir / "Dockerfile").exists():
            raise RuntimeError(
                f"Container image '{image}' not found and "
                f"no Dockerfile at {container_dir / 'Dockerfile'}"
            )
        logger.info("Container image not found, building...", image=image)
        try:
            build = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is selected by trusted runtime detection and argv is fixed.
                [runtime.cli, "build", "-t", image, "."],
                cwd=str(container_dir),
                check=False,
            )
        finally:
            cleanup_runtime_builder(runtime)
        if build.returncode != 0:
            raise RuntimeError(f"Failed to build container image '{image}'")

    reap_orphaned_agent_containers(runtime=runtime)
    cleanup_runtime_images(runtime)
