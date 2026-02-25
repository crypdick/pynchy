"""MCP instance lifecycle — Docker container and script subprocess management.

Standalone functions extracted from :class:`McpManager` so the manager
module stays focused on resolution, sync, and workspace mapping.  Each
function operates on a single :class:`McpInstance` and has no reference
to the manager class itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pynchy.container_runner._docker import (
    ensure_image,
    ensure_network,
    is_container_running,
    remove_container,
    run_docker,
    stop_container,
    wait_healthy,
)
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.config_mcp import McpServerConfig
    from pynchy.container_runner.mcp_manager import McpInstance

_NETWORK_NAME = "pynchy-litellm-net"


# ---------------------------------------------------------------------------
# Docker lifecycle
# ---------------------------------------------------------------------------


async def ensure_docker_running(instance: McpInstance) -> None:
    """Start a Docker MCP container if not already running."""
    if is_container_running(instance.container_name):
        return

    logger.info(
        "Starting MCP container on-demand",
        instance_id=instance.instance_id,
        container=instance.container_name,
        image=instance.server_config.image,
    )

    _ensure_mcp_image(instance.server_config)
    ensure_network(_NETWORK_NAME)

    # Remove stale container
    remove_container(instance.container_name)

    # Build container args
    cmd_args = list(instance.server_config.args)
    cmd_args.extend(kwargs_to_args(instance.kwargs))

    # Publish port so the host can health-check the container.
    # endpoint_url uses the Docker-internal container name (for LiteLLM),
    # but the health check runs from the host which can't resolve those.
    port = instance.server_config.port
    publish_args = ["-p", f"{port}:{port}"] if port else []
    for extra_port in instance.server_config.extra_ports:
        publish_args.extend(["-p", f"{extra_port}:{extra_port}"])

    # Build -e flags from static env and env_forward on the server definition
    env_args = build_env_args(instance.server_config)

    # Build -v flags from volumes, resolving relative host paths from project root.
    # Docker named volumes (no "/" or "." in the name, e.g. "mcp-gdrive:/data")
    # are passed through as-is; only host paths get resolved and mkdir'd.
    # Expand {key} placeholders using instance kwargs (e.g.,
    # "groups/{workspace}:/workspace" → "groups/research:/workspace").
    volume_args: list[str] = []
    for vol in instance.server_config.volumes:
        for key, value in instance.kwargs.items():
            vol = vol.replace(f"{{{key}}}", value)
        host_path, sep, container_path = vol.partition(":")
        if sep and "/" not in host_path and not host_path.startswith("."):
            # Docker named volume — pass through without resolution
            volume_args.extend(["-v", vol])
        elif sep and not Path(host_path).is_absolute():
            from pynchy.config import get_settings

            host_path = str(get_settings().project_root / host_path)
            _ensure_mount_parent(host_path)
            volume_args.extend(["-v", f"{host_path}:{container_path}"])
        else:
            if sep:
                _ensure_mount_parent(host_path)
            volume_args.extend(["-v", vol])

    run_docker(
        "run", "-d",
        "--name", instance.container_name,
        "--network", _NETWORK_NAME,
        "--restart", "unless-stopped",
        *publish_args,
        *env_args,
        *volume_args,
        instance.server_config.image or "",
        *cmd_args,
    )  # fmt: skip

    # Health-check via localhost (host-side), not the Docker-internal name
    health_url = f"http://localhost:{port}" if port else instance.endpoint_url
    try:
        await wait_healthy(
            instance.container_name,
            health_url,
            any_non_5xx=True,
        )
    except (TimeoutError, RuntimeError):
        logger.error(
            "MCP container failed health check",
            instance_id=instance.instance_id,
            container=instance.container_name,
        )
        # Clean up the failed container (matches script path which
        # calls terminate_process before re-raising).
        stop_container(instance.container_name)
        raise

    logger.info("MCP container ready", instance_id=instance.instance_id)


# ---------------------------------------------------------------------------
# Script lifecycle
# ---------------------------------------------------------------------------


async def ensure_script_running(instance: McpInstance) -> None:
    """Start a script MCP subprocess if not already running."""
    if instance.process is not None and instance.process.poll() is None:
        return  # still alive

    cfg = instance.server_config
    cmd = [cfg.command or "", *cfg.args]
    cmd.extend(kwargs_to_args(instance.kwargs))

    # Merge env: inherit host env + static env + env_forward
    merged_env = {**os.environ, **cfg.env}
    merged_env.update(resolve_env_forward(cfg.env_forward))

    logger.info(
        "Starting MCP script on-demand",
        instance_id=instance.instance_id,
        command=cmd,
    )

    instance.process = subprocess.Popen(
        cmd,
        env=merged_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,  # own process group for clean shutdown
    )

    # Health-check via localhost
    health_url = f"http://localhost:{cfg.port}"
    try:
        await wait_healthy(
            instance.instance_id,
            health_url,
            any_non_5xx=True,
            process=instance.process,
        )
    except (TimeoutError, RuntimeError):
        stderr_tail = ""
        if instance.process.stderr:
            with contextlib.suppress(OSError, ValueError):
                stderr_tail = instance.process.stderr.read(2000).decode(errors="replace")
        logger.error(
            "MCP script failed health check",
            instance_id=instance.instance_id,
            stderr=stderr_tail,
        )
        terminate_process(instance)
        raise

    logger.info("MCP script ready", instance_id=instance.instance_id)


# ---------------------------------------------------------------------------
# Image warm-up
# ---------------------------------------------------------------------------


async def warm_image_cache(instances: dict[str, McpInstance]) -> None:
    """Pre-pull/build Docker images for all MCP instances in the background."""
    seen: set[str] = set()
    for inst in instances.values():
        cfg = inst.server_config
        if cfg.type != "docker" or not cfg.image or cfg.image in seen:
            continue
        seen.add(cfg.image)
        try:
            await asyncio.to_thread(_ensure_mcp_image, cfg)
            logger.info("Warmed MCP image cache", image=cfg.image)
        except Exception:
            logger.exception("Failed to warm MCP image", image=cfg.image)


# ---------------------------------------------------------------------------
# Process management helpers
# ---------------------------------------------------------------------------


def terminate_process(instance: McpInstance) -> None:
    """SIGTERM a script MCP subprocess, escalating to SIGKILL after 5s."""
    proc = instance.process
    if proc is None or proc.poll() is not None:
        instance.process = None
        return
    try:
        # Send SIGTERM to the process group (start_new_session=True)
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
    except (ProcessLookupError, OSError):
        pass  # already dead
    instance.process = None


# ---------------------------------------------------------------------------
# Arg / env helpers
# ---------------------------------------------------------------------------


def kwargs_to_args(kwargs: dict[str, str]) -> list[str]:
    """Convert kwargs dict to Docker command args (``--key value`` pairs)."""
    args: list[str] = []
    for key, value in sorted(kwargs.items()):
        args.extend([f"--{key}", value])
    return args


def resolve_env_forward(env_forward: dict[str, str]) -> dict[str, str]:
    """Resolve ``env_forward`` mappings to concrete values from the host environment.

    Returns ``{container_var: resolved_value}`` for each host var that exists.
    Logs a warning for any host variable that is not set.
    """
    resolved: dict[str, str] = {}
    for container_var, host_var in sorted(env_forward.items()):
        value = os.environ.get(host_var)
        if value is None:
            logger.warning(
                "env_forward var not set on host — skipping",
                container_var=container_var,
                host_var=host_var,
            )
            continue
        resolved[container_var] = value
    return resolved


def build_env_args(config: McpServerConfig) -> list[str]:
    """Build ``-e KEY=VALUE`` Docker flags from ``env`` and ``env_forward``.

    ``env_forward`` is a ``{container_var: host_var}`` dict (normalized from
    list or dict form by the Pydantic validator).
    """
    args: list[str] = []
    for key, value in sorted(config.env.items()):
        args.extend(["-e", f"{key}={value}"])
    for container_var, value in resolve_env_forward(config.env_forward).items():
        args.extend(["-e", f"{container_var}={value}"])
    return args


def _ensure_mcp_image(config: McpServerConfig) -> None:
    """Ensure the MCP Docker image exists — build from local Dockerfile or pull.

    When ``config.dockerfile`` is set and the image isn't already local,
    builds it from the specified Dockerfile. Otherwise falls back to pulling
    from a registry via :func:`ensure_image`.
    """
    from pynchy.config import get_settings

    image = config.image or ""
    if config.dockerfile:
        # Check if image already exists locally
        result = run_docker("image", "inspect", image, check=False)
        if result.returncode == 0:
            return
        # Build from local Dockerfile
        project_root = str(get_settings().project_root)
        dockerfile_path = str(get_settings().project_root / config.dockerfile)
        logger.info(
            "Building MCP image from local Dockerfile",
            image=image,
            dockerfile=config.dockerfile,
        )
        run_docker(
            "build", "-t", image,
            "-f", dockerfile_path,
            project_root,
            timeout=300,
        )  # fmt: skip
        logger.info("MCP image built", image=image)
    else:
        ensure_image(image)


def _ensure_mount_parent(host_path: str) -> None:
    """Ensure mount source exists — mkdir for directories, parent-mkdir for files."""
    p = Path(host_path)
    if p.exists():
        return  # already exists (file or directory)
    # Heuristic: paths with file extensions are files, others are directories.
    if p.suffix:
        p.parent.mkdir(parents=True, exist_ok=True)
    else:
        p.mkdir(parents=True, exist_ok=True)
