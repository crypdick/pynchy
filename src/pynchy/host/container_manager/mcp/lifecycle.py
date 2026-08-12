"""MCP instance lifecycle — Docker container and script subprocess management.

Standalone functions extracted from :class:`McpManager` so the manager
module stays focused on sync and workspace mapping.  Each function
operates on a single :class:`McpInstance` and has no reference to the
manager class itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess  # noqa: S404 - MCP lifecycle starts configured no-shell processes.
import sys
import time
from collections.abc import (  # noqa: TC003 - beartype resolves MCP environment annotations at runtime.
    Mapping,
)
from pathlib import Path

from pynchy.atomic_json import write_json_atomic
from pynchy.host.container_manager.docker import (
    HealthCheckRequest,
    ensure_image,
    ensure_network,
    is_container_running,
    redacted_container_logs,
    remove_container,
    run_docker,
    stop_container,
    wait_healthy,
)
from pynchy.host.container_manager.mcp.resolution import (
    McpInstance,  # noqa: TC001 - beartype resolves MCP lifecycle signatures at runtime.
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    McpServerConfig,  # noqa: TC001 - beartype resolves MCP lifecycle signatures at runtime.
)
from pynchy.process_environment import filtered_process_environment
from pynchy.runtime_names import runtime_network_name

_PROCESS_MARKER = re.compile(r"pynchy-mcp-[0-9a-f]{32}")
_PROCESS_SUPERVISOR_SCRIPT = '"$@" &\nchild=$!\nwait "$child"\n'
_PROCESS_STOP_TIMEOUT_SECONDS = 5.0

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

    await _ensure_mcp_image(instance.server_config, instance.project_root)
    await ensure_network(runtime_network_name("litellm-net"))

    # Remove stale container
    await remove_container(instance.container_name)

    placeholders = _build_placeholders(instance)
    await _start_docker_container(instance, placeholders)
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

    merged_env = filtered_process_environment({**cfg.env, **instance.tool_environment})
    logger.info(
        "Starting MCP script on-demand",
        instance_id=instance.instance_id,
        command=cmd,
    )

    await asyncio.to_thread(_start_owned_process, instance, cmd, merged_env)

    # Health-check via localhost using instance port (unique per workspace)
    health_url = f"http://localhost:{instance.port}"
    try:
        await wait_healthy(
            HealthCheckRequest(
                container_name=instance.instance_id,
                url=health_url,
                any_non_5xx=True,
                process=instance.process,
                health_timeout_seconds=instance.server_config.startup_timeout_seconds,
            )
        )
    except (TimeoutError, RuntimeError):
        logger.error(
            "MCP script failed health check",
            instance_id=instance.instance_id,
        )
        await asyncio.to_thread(terminate_process, instance)
        raise

    logger.info("MCP script ready", instance_id=instance.instance_id)


# ---------------------------------------------------------------------------
# Stdio lifecycle
# ---------------------------------------------------------------------------


async def ensure_stdio_running(instance: McpInstance) -> None:
    """Start a loopback HTTP bridge for a configured stdio MCP server."""
    if instance.process is not None and instance.process.poll() is None:
        return

    if instance.port is None:
        raise RuntimeError(f"Stdio MCP has no host port: {instance.instance_id}")

    cmd = _stdio_bridge_command(instance)
    logger.info(
        "Starting MCP stdio bridge on-demand",
        instance_id=instance.instance_id,
        command=cmd,
    )
    await asyncio.to_thread(
        _start_owned_process,
        instance,
        cmd,
        build_stdio_env(instance.server_config, instance.tool_environment),
    )

    try:
        await wait_healthy(
            HealthCheckRequest(
                container_name=instance.instance_id,
                url=f"http://localhost:{instance.port}",
                any_non_5xx=True,
                process=instance.process,
                health_timeout_seconds=instance.server_config.startup_timeout_seconds,
            )
        )
    except (TimeoutError, RuntimeError):
        logger.error("MCP stdio bridge failed health check", instance_id=instance.instance_id)
        await asyncio.to_thread(terminate_process, instance)
        raise

    logger.info("MCP stdio bridge ready", instance_id=instance.instance_id)


def _start_script_process(
    cmd: list[str],
    env: dict[str, str],
    marker: str,
) -> subprocess.Popen[bytes]:
    shell = shutil.which("sh")
    if shell is None:
        raise RuntimeError("MCP process supervision requires sh")
    return subprocess.Popen(  # noqa: S603 - MCP script command comes from trusted config and runs without a shell.
        [shell, "-c", _PROCESS_SUPERVISOR_SCRIPT, marker, *cmd],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,  # own process group for clean shutdown
    )


def _start_owned_process(
    instance: McpInstance,
    cmd: list[str],
    env: dict[str, str],
) -> None:
    """Persist exact process-group ownership before readiness can block or crash."""
    marker = f"pynchy-mcp-{secrets.token_hex(16)}"
    process = _start_script_process(cmd, env, marker)
    instance.process = process
    instance.process_marker = marker
    record_path = instance.process_record_path
    if record_path is None:
        return
    try:
        write_json_atomic(
            record_path,
            {
                "version": 1,
                "pid": process.pid,
                "marker": marker,
                "instance_id": instance.instance_id,
            },
        )
    except OSError:
        terminate_process(instance)
        raise


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
            await _ensure_mcp_image(cfg, inst.project_root)
            logger.info("Warmed MCP image cache", image=cfg.image)
        except Exception:  # noqa: BLE001 - image warm-up is best-effort and must not block boot.
            logger.exception("Failed to warm MCP image", image=cfg.image)


# ---------------------------------------------------------------------------
# Process management helpers
# ---------------------------------------------------------------------------


def terminate_process(instance: McpInstance) -> None:
    """SIGTERM a script MCP subprocess, escalating to SIGKILL after 5s."""
    proc = instance.process
    if proc is None or proc.poll() is not None:
        instance.process = None
        instance.process_marker = None
        _remove_process_record(instance.process_record_path)
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        _terminate_process_group(proc)
    instance.process = None
    instance.process_marker = None
    _remove_process_record(instance.process_record_path)


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    # Send SIGTERM to the process group (start_new_session=True)
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=2)


def reap_stale_processes(record_dir: Path) -> int:
    """Stop process groups proven to belong to an exited Pynchy process."""
    reaped = 0
    if not record_dir.is_dir():
        return reaped
    for record_path in sorted(record_dir.glob("*.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _remove_process_record(record_path)
            continue
        pid = record.get("pid") if isinstance(record, dict) else None
        marker = record.get("marker") if isinstance(record, dict) else None
        if (
            isinstance(pid, int)
            and pid > 1
            and isinstance(marker, str)
            and _process_has_marker(pid, marker)
        ):
            _terminate_owned_process_group(pid)
            reaped += 1
        _remove_process_record(record_path)
    return reaped


def _process_has_marker(pid: int, marker: str) -> bool:
    if _PROCESS_MARKER.fullmatch(marker) is None:
        return False
    ps = shutil.which("ps")
    if ps is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - fixed process-status probe with validated PID.
            [ps, "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and marker in result.stdout


def _terminate_owned_process_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + _PROCESS_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_group_exists(pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _process_group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_process_record(record_path: Path | None) -> None:
    if record_path is not None:
        record_path.unlink(missing_ok=True)


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


def _resolved_volume_arg(vol: str, project_root: Path) -> list[str]:
    host_path, sep, container_path = vol.partition(":")
    if sep and "/" not in host_path and not host_path.startswith("."):
        # Docker named volume — pass through without resolution
        return ["-v", vol]
    if sep and not Path(host_path).is_absolute():
        host_path = str(project_root / host_path)
        _ensure_mount_parent(host_path)
        return ["-v", f"{host_path}:{container_path}"]
    if sep:
        _ensure_mount_parent(host_path)
    return ["-v", vol]


def _docker_volume_args(
    instance: McpInstance,
    placeholders: dict[str, str],
) -> list[str]:
    args: list[str] = []
    for vol in instance.server_config.volumes:
        volume = _expanded_volume_spec(vol, placeholders)
        args.extend(_resolved_volume_arg(volume, instance.project_root))
    return args


async def _start_docker_container(
    instance: McpInstance,
    placeholders: dict[str, str],
) -> None:
    container_environment = {**instance.server_config.env, **instance.tool_environment}
    await run_docker(
        "run", "-d",
        "--name", instance.container_name,
        "--network", runtime_network_name("litellm-net"),
        "--restart", "unless-stopped",
        *_docker_publish_args(instance),
        *build_env_args(container_environment),
        *_docker_volume_args(instance, placeholders),
        instance.server_config.image or "",
        *_docker_command_args(instance, placeholders),
        environment=filtered_process_environment(container_environment),
    )  # fmt: skip


def _docker_health_url(instance: McpInstance) -> str:
    return f"http://localhost:{instance.port}" if instance.port else instance.endpoint_url


async def _wait_for_docker_health(instance: McpInstance) -> None:
    try:
        await wait_healthy(
            HealthCheckRequest(
                container_name=instance.container_name,
                url=_docker_health_url(instance),
                any_non_5xx=True,
                health_timeout_seconds=instance.server_config.startup_timeout_seconds,
            )
        )
    except (TimeoutError, RuntimeError) as exc:
        log_result = await run_docker("logs", "--tail", "50", instance.container_name, check=False)
        logger.error(
            "MCP container failed health check",
            instance_id=instance.instance_id,
            container=instance.container_name,
            error_type=type(exc).__name__,
            log_tail=redacted_container_logs(log_result, limit=4000),
        )
        # Clean up the failed container (matches script path which
        # calls terminate_process before re-raising). Capture diagnostics
        # first: docker rm makes a timeout otherwise impossible to diagnose.
        await stop_container(instance.container_name, stop_timeout_seconds=1)
        raise


def kwargs_to_args(kwargs: dict[str, str]) -> list[str]:
    """Convert kwargs dict to Docker command args (``--key value`` pairs)."""
    args: list[str] = []
    for key, value in sorted(kwargs.items()):
        args.extend([f"--{key}", value])
    return args


def build_stdio_env(
    config: McpServerConfig,
    tool_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the intentionally small host environment for a stdio bridge.

    Host tooling gets only the operational process baseline, static runtime
    configuration, and variables declared by the selected tool.
    """
    return filtered_process_environment({**config.env, **(tool_environment or {})})


def _stdio_bridge_command(instance: McpInstance) -> list[str]:
    placeholders = _build_placeholders(instance)
    args = expand_arg_placeholders(list(instance.server_config.args), placeholders)
    return [
        sys.executable,
        "-m",
        "pynchy.host.container_manager.mcp.stdio_bridge",
        "--port",
        str(instance.port),
        "--",
        instance.server_config.command or "",
        *args,
        *kwargs_to_args(instance.kwargs),
    ]


def build_env_args(
    environment: Mapping[str, str],
) -> list[str]:
    """Build value-free Docker flags for explicitly selected variables."""
    args: list[str] = []
    for key in sorted(environment):
        args.extend(["-e", key])
    return args


async def _ensure_mcp_image(config: McpServerConfig, project_root_path: Path) -> None:
    """Ensure the MCP Docker image exists — build from local Dockerfile or pull.

    When ``config.dockerfile`` is set and the image isn't already local,
    builds it from the specified Dockerfile. Otherwise falls back to pulling
    from a registry via :func:`ensure_image`.
    """
    image = config.image or ""
    if config.dockerfile:
        # Check if image already exists locally
        result = await run_docker("image", "inspect", image, check=False)
        if result.returncode == 0:
            return
        # Build from local Dockerfile
        dockerfile_path = str(project_root_path / config.dockerfile)
        build_context = str(project_root_path / config.build_context)
        logger.info(
            "Building MCP image from local Dockerfile",
            image=image,
            dockerfile=config.dockerfile,
        )
        await run_docker(
            "build", "-t", image,
            "-f", dockerfile_path,
            build_context,
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
