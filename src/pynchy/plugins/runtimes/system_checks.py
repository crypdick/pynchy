"""System checks and setup for external dependencies (containers)."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - system checks use fixed no-shell runtime CLI argv.
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import (
    Path,
)
from typing import cast

from pynchy.identifiers import (
    OrphanReapAgeMs,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    RuntimeProvider,
)
from pynchy.plugins.runtimes.apple_build_lock import apple_build_lock
from pynchy.plugins.runtimes.cleanup import (
    OrphanReapingRuntime,
    cleanup_runtime_build_state,
    cleanup_runtime_builder,
    reap_orphaned_agent_containers,
)
from pynchy.plugins.runtimes.detection import get_runtime

_MISSING_IMAGE_AND_DOCKERFILE_ERROR = (
    "Container image '{image}' not found and no Dockerfile at {dockerfile}"
)
_CONTAINER_BUILD_FAILED_ERROR = "Failed to build container image '{image}'"
_CONTAINER_BUILD_STATE_CLEANUP_FAILED_ERROR = (
    "Failed to clean stale container build state before building image '{image}'"
)
_PLUGIN_REQUIREMENTS_GENERATION_FAILED_ERROR = "Failed to generate container plugin requirements"
_RUNTIME_HARNESS_ENV = "PYNCHY_RUNTIME_HARNESS"
_AGENT_IMAGE_LOCK = threading.Lock()
_RUNTIME_INSPECT_TIMEOUT_SECONDS = 30


def _generate_plugin_requirements(container_dir: Path, project_root: Path) -> None:
    """Write the plugin requirements file consumed by the agent Dockerfile."""
    generator = container_dir / "scripts" / "generate_plugin_requirements.py"
    requirements = container_dir / "requirements-plugins.txt"
    result = subprocess.run(  # noqa: S603 - host Python runs a repository-local generator with fixed argv.
        [
            sys.executable,
            str(generator),
            "--output",
            str(requirements),
            "--config",
            str(project_root / "data" / "personalization" / "pynchy.toml"),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(_PLUGIN_REQUIREMENTS_GENERATION_FAILED_ERROR)


def _ensure_agent_image_available(runtime: object, *, project_root: Path, image: str) -> None:
    """Build the configured agent image when it is absent from the runtime."""
    runtime_cli = cast("RuntimeProvider", runtime).cli
    result = subprocess.run(  # noqa: S603 - runtime CLI is selected by trusted runtime detection and argv is fixed.
        [runtime_cli, "image", "inspect", image],
        capture_output=True,
        check=False,
        timeout=_RUNTIME_INSPECT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        if not cleanup_runtime_build_state(runtime):
            raise RuntimeError(_CONTAINER_BUILD_STATE_CLEANUP_FAILED_ERROR.format(image=image))
        container_dir = project_root / "src" / "pynchy" / "agent"
        dockerfile = container_dir / "Dockerfile"
        if not dockerfile.exists():
            raise RuntimeError(
                _MISSING_IMAGE_AND_DOCKERFILE_ERROR.format(image=image, dockerfile=dockerfile)
            )
        _generate_plugin_requirements(container_dir, project_root)
        logger.info("Container image not found, building...", image=image)
        try:
            build = subprocess.run(  # noqa: S603 - runtime CLI is selected by trusted runtime detection and argv is fixed.
                [runtime_cli, "build", "-t", image, "."],
                cwd=str(container_dir),
                check=False,
            )
        finally:
            cleanup_runtime_build_state(runtime)
        if build.returncode != 0:
            cleanup_runtime_builder(runtime)
            raise RuntimeError(_CONTAINER_BUILD_FAILED_ERROR.format(image=image))


def ensure_agent_image_available(*, project_root: Path, image: str) -> None:
    """Verify that the configured agent image is ready to launch.

    This runs immediately before agent spawning so the deterministic runtime
    harness can exercise host services without eagerly building an agent image.
    """
    runtime = get_runtime()
    runtime.ensure_running()
    with _AGENT_IMAGE_LOCK, _runtime_build_lock(runtime):
        _ensure_agent_image_available(runtime, project_root=project_root, image=image)


@contextmanager
def _runtime_build_lock(runtime: object) -> Iterator[None]:
    """Lock only Apple Container, whose builder is shared between processes."""
    if getattr(runtime, "cli", "") == "container":
        with apple_build_lock():
            yield
    else:
        yield


def _orphan_reaping_runtime(runtime: object) -> OrphanReapingRuntime | None:
    """Parse an optional reaping capability from a selected runtime plugin."""
    if all(
        callable(getattr(runtime, name, None)) for name in ("list_containers", "remove_container")
    ):
        return cast("OrphanReapingRuntime", runtime)
    return None


def ensure_container_system_running(
    orphan_reap_age_ms: OrphanReapAgeMs,
    *,
    project_root: Path,
    image: str,
) -> None:
    """Verify the container runtime and clean up stale agent resources."""
    runtime = get_runtime()
    runtime.ensure_running()
    with _runtime_build_lock(runtime):
        cleanup_runtime_build_state(runtime)
        if os.environ.get(_RUNTIME_HARNESS_ENV) != "1":
            with _AGENT_IMAGE_LOCK:
                _ensure_agent_image_available(runtime, project_root=project_root, image=image)

    if orphan_reaper := _orphan_reaping_runtime(runtime):
        reap_orphaned_agent_containers(
            runtime=orphan_reaper,
            orphan_age_ms=orphan_reap_age_ms,
            active_names=set(),
        )
