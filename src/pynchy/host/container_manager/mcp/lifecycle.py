"""MCP instance lifecycle — Docker container and script subprocess management.

Standalone functions extracted from :class:`McpManager` so the manager
module stays focused on sync and workspace mapping.  Each function
operates on a single :class:`McpInstance` and has no reference to the
manager class itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from pathlib import Path

from pynchy.config.mcp import McpServerConfig
from pynchy.host.container_manager.docker import (
    ensure_image,
    ensure_network,
    is_container_running,
    remove_container,
    run_docker,
    stop_container,
    wait_healthy,
)
from pynchy.host.container_manager.mcp.resolution import McpInstance
from pynchy.host.container_manager.onecli import OneCliMaterial, prepare_onecli_material
from pynchy.logger import logger
from pynchy.types import VolumeMount

_NETWORK_NAME = "pynchy-litellm-net"


# ---------------------------------------------------------------------------
# Docker lifecycle
# ---------------------------------------------------------------------------


async def ensure_docker_running(instance: McpInstance) -> None:
    """Start a Docker MCP container if not already running."""
    if await is_container_running(instance.container_name):
        return

    logger.info(
        "Starting MCP container on-demand",
        instance_id=instance.instance_id,
        container=instance.container_name,
        image=instance.server_config.image,
    )

    await _ensure_mcp_image(instance.server_config)
    await ensure_network(_NETWORK_NAME)

    # Remove stale container
    await remove_container(instance.container_name)

    placeholders = _build_placeholders(instance)
    onecli_material = _prepare_instance_onecli_material(instance)
    await _start_docker_container(instance, placeholders, onecli_material)
    await _wait_for_docker_health(instance)

    logger.info("MCP container ready", instance_id=instance.instance_id)


# ---------------------------------------------------------------------------
# Script lifecycle
# ---------------------------------------------------------------------------


async def ensure_script_running(instance: McpInstance) -> None:
    """Start a script MCP subprocess if not already running."""
    if instance.process is not None and instance.process.poll() is None:
        return  # still alive

    cfg = instance.server_config
    # Expand {key} placeholders (e.g. {port}, {workspace}) in args
    placeholders = _build_placeholders(instance)
    expanded_args = expand_arg_placeholders(list(cfg.args), placeholders)
    cmd = [cfg.command or "", *expanded_args]
    cmd.extend(kwargs_to_args(instance.kwargs))

    # Merge env: inherit host env + static env + env_forward
    merged_env = {**os.environ, **cfg.env}
    merged_env.update(resolve_env_forward(cfg.env_forward))
    onecli_material = _prepare_instance_onecli_material(instance)
    if onecli_material:
        merged_env.update(_host_process_env(onecli_material))

    logger.info(
        "Starting MCP script on-demand",
        instance_id=instance.instance_id,
        command=cmd,
    )

    instance.process = await asyncio.to_thread(_start_script_process, cmd, merged_env)

    # Health-check via localhost using instance port (unique per workspace)
    health_url = f"http://localhost:{instance.port}"
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


def _start_script_process(
    cmd: list[str],
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,  # own process group for clean shutdown
    )


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
            await _ensure_mcp_image(cfg)
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


def expand_arg_placeholders(args: list[str], placeholders: dict[str, str]) -> list[str]:
    """Substitute ``{key}`` placeholders in *args* with values from *placeholders*.

    Mirrors the volume-mount placeholder syntax so plugins can use
    ``{port}``, ``{workspace}``, etc. in command args.
    Unrecognised placeholders are left as-is.
    """
    expanded: list[str] = []
    for arg in args:
        expanded_arg = arg
        for key, value in placeholders.items():
            expanded_arg = expanded_arg.replace(f"{{{key}}}", value)
        expanded.append(expanded_arg)
    return expanded


def _build_placeholders(instance: McpInstance) -> dict[str, str]:
    """Build the placeholder dict for arg/volume expansion.

    Includes instance kwargs (e.g. ``workspace``) plus ``port``.
    """
    placeholders = dict(instance.kwargs)
    if instance.port is not None:
        placeholders["port"] = str(instance.port)
    return placeholders


def _docker_command_args(instance: McpInstance, placeholders: dict[str, str]) -> list[str]:
    args = expand_arg_placeholders(list(instance.server_config.args), placeholders)
    args.extend(kwargs_to_args(instance.kwargs))
    return args


def _docker_publish_args(instance: McpInstance) -> list[str]:
    # Publish port so the host can health-check the container.
    # endpoint_url uses the Docker-internal container name (for LiteLLM),
    # but the health check runs from the host which can't resolve those.
    # Host-side port comes from instance.port (unique per workspace);
    # container-internal port stays at cfg.port (no conflict inside container).
    host_port = instance.port
    container_port = instance.server_config.port
    args = ["-p", f"{host_port}:{container_port}"] if host_port else []
    for extra_port in instance.server_config.extra_ports:
        args.extend(["-p", f"{extra_port}:{extra_port}"])
    return args


def _expanded_volume_spec(vol: str, placeholders: dict[str, str]) -> str:
    for key, value in placeholders.items():
        vol = vol.replace(f"{{{key}}}", value)
    return vol


def _resolved_volume_arg(vol: str) -> list[str]:
    host_path, sep, container_path = vol.partition(":")
    if sep and "/" not in host_path and not host_path.startswith("."):
        # Docker named volume — pass through without resolution
        return ["-v", vol]
    if sep and not Path(host_path).is_absolute():
        from pynchy.config import get_settings

        host_path = str(get_settings().project_root / host_path)
        _ensure_mount_parent(host_path)
        return ["-v", f"{host_path}:{container_path}"]
    if sep:
        _ensure_mount_parent(host_path)
    return ["-v", vol]


def _docker_volume_args(
    instance: McpInstance,
    placeholders: dict[str, str],
    onecli_material: OneCliMaterial | None,
) -> list[str]:
    args: list[str] = []
    for vol in instance.server_config.volumes:
        args.extend(_resolved_volume_arg(_expanded_volume_spec(vol, placeholders)))
    if onecli_material:
        args.extend(_mounts_to_docker_args(onecli_material.mounts))
    return args


async def _start_docker_container(
    instance: McpInstance,
    placeholders: dict[str, str],
    onecli_material: OneCliMaterial | None,
) -> None:
    await run_docker(
        "run", "-d",
        "--name", instance.container_name,
        "--network", _NETWORK_NAME,
        "--restart", "unless-stopped",
        *_docker_publish_args(instance),
        *build_env_args(
            instance.server_config,
            extra_env=onecli_material.env_vars if onecli_material else None,
        ),
        *_docker_volume_args(instance, placeholders, onecli_material),
        instance.server_config.image or "",
        *_docker_command_args(instance, placeholders),
    )  # fmt: skip


def _docker_health_url(instance: McpInstance) -> str:
    return f"http://localhost:{instance.port}" if instance.port else instance.endpoint_url


async def _wait_for_docker_health(instance: McpInstance) -> None:
    try:
        await wait_healthy(
            instance.container_name,
            _docker_health_url(instance),
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
        await stop_container(instance.container_name)
        raise


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


def build_env_args(
    config: McpServerConfig,
    *,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    """Build ``-e KEY=VALUE`` Docker flags from ``env`` and ``env_forward``.

    ``env_forward`` is a ``{container_var: host_var}`` dict (normalized from
    list or dict form by the Pydantic validator).
    """
    args: list[str] = []
    for key, value in sorted(config.env.items()):
        args.extend(["-e", f"{key}={value}"])
    for container_var, value in resolve_env_forward(config.env_forward).items():
        args.extend(["-e", f"{container_var}={value}"])
    for key, value in sorted((extra_env or {}).items()):
        args.extend(["-e", f"{key}={value}"])
    return args


def _prepare_instance_onecli_material(instance: McpInstance) -> OneCliMaterial | None:
    cfg = instance.server_config
    if not cfg.onecli:
        return None
    if cfg.onecli_agent == "workspace":
        group_folder = instance.kwargs.get("workspace", instance.server_name)
    else:
        group_folder = cfg.onecli_agent
    return prepare_onecli_material(group_folder)


def _mounts_to_docker_args(mounts: list[VolumeMount]) -> list[str]:
    args: list[str] = []
    for mount in mounts:
        suffix = ":ro" if mount.readonly else ""
        args.extend(["-v", f"{mount.host_path}:{mount.container_path}{suffix}"])
    return args


def _host_process_env(material: OneCliMaterial) -> dict[str, str]:
    host_paths_by_container_path = {
        mount.container_path: mount.host_path for mount in material.mounts
    }
    return {
        key: host_paths_by_container_path.get(value, value)
        for key, value in material.env_vars.items()
    }


async def _ensure_mcp_image(config: McpServerConfig) -> None:
    """Ensure the MCP Docker image exists — build from local Dockerfile or pull.

    When ``config.dockerfile`` is set and the image isn't already local,
    builds it from the specified Dockerfile. Otherwise falls back to pulling
    from a registry via :func:`ensure_image`.
    """
    from pynchy.config import get_settings

    image = config.image or ""
    if config.dockerfile:
        # Check if image already exists locally
        result = await run_docker("image", "inspect", image, check=False)
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
        await run_docker(
            "build", "-t", image,
            "-f", dockerfile_path,
            project_root,
            command_timeout_seconds=300,
        )  # fmt: skip
        logger.info("MCP image built", image=image)
    else:
        await ensure_image(image)


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
