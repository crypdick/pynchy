"""Shared Docker helpers — subprocess wrappers used by gateway and MCP manager.

Extracted from :mod:`pynchy.host.container_manager.gateway` so that both
:class:`LiteLLMGateway` and :class:`McpManager` can share them.

All public functions are async so they don't block the event loop.
The underlying subprocess calls run in a thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess  # noqa: S404 - Docker helpers use fixed no-shell argv.
import time
from collections.abc import (  # noqa: TC003 - beartype resolves Docker environment annotations at runtime.
    Mapping,
)
from dataclasses import dataclass

import aiohttp

from pynchy.logger import logger
from pynchy.process_environment import filtered_process_environment
from pynchy.redaction import irreversibly_redact


def docker_available() -> bool:
    """Check if ``docker`` is on PATH."""
    return shutil.which("docker") is not None


@dataclass(frozen=True)
class HealthCheckRequest:
    """Parameters for waiting on a container or local process health check."""

    container_name: str
    url: str
    health_timeout_seconds: float = 90
    poll_interval: float = 1.0
    headers: dict[str, str] | None = None
    any_non_5xx: bool = False
    process: subprocess.Popen[bytes] | None = None


def _run_docker_sync(
    *args: str,
    check: bool = True,
    timeout: int = 30,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``docker`` CLI command (blocking — internal only)."""
    return subprocess.run(  # noqa: S603 - args are constrained by internal Docker helper call sites; no shell.
        ["docker", *args],  # noqa: S607 - docker is the trusted runtime CLI for this helper.
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env=filtered_process_environment(dict(environment or {})),
    )


async def run_docker(
    *args: str,
    check: bool = True,
    command_timeout_seconds: int = 30,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``docker`` CLI command without blocking the event loop."""
    return await asyncio.to_thread(
        _run_docker_sync,
        *args,
        check=check,
        timeout=command_timeout_seconds,
        environment=environment,
    )


def redacted_container_logs(result: subprocess.CompletedProcess[str], *, limit: int) -> str:
    """Return bounded container diagnostics without retaining detected secrets."""
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return irreversibly_redact(combined)[-limit:] if combined else "(no container log output)"


async def ensure_image(image: str) -> None:
    """Pull a Docker image if not already present locally."""
    result = await run_docker("image", "inspect", image, check=False)
    if result.returncode == 0:
        return

    logger.info("Pulling Docker image (first run may take a minute)", image=image)
    await run_docker("pull", image, command_timeout_seconds=300)
    logger.info("Docker image pulled", image=image)


async def ensure_network(name: str) -> None:
    """Create a Docker network if it doesn't already exist."""
    result = await run_docker("network", "inspect", name, check=False)
    if result.returncode == 0:
        return
    await run_docker("network", "create", name)
    logger.info("Created Docker network", network=name)


async def is_container_running(name: str) -> bool:
    """Check if a Docker container is currently running."""
    start = time.monotonic()
    result = await run_docker("inspect", "-f", "{{.State.Running}}", name, check=False)
    elapsed_ms = (time.monotonic() - start) * 1000
    if elapsed_ms > 500:
        logger.warning(
            "Slow docker inspect",
            container=name,
            elapsed_ms=round(elapsed_ms),
        )
    return result.stdout.strip() == "true"


async def remove_container(name: str) -> None:
    """Force-remove a container (idempotent, no error if absent).

    Use before starting a container to clear stale state.
    """
    await run_docker("rm", "-f", name, check=False)


async def stop_container(name: str, *, stop_timeout_seconds: int = 5) -> None:
    """Gracefully stop a container then force-remove it.

    Sends SIGTERM (docker stop) with a grace period, then removes
    the container so it doesn't linger as "exited".  Idempotent —
    safe to call even if the container is already stopped or absent.
    """
    await run_docker("stop", "-t", str(stop_timeout_seconds), name, check=False)
    await run_docker("rm", "-f", name, check=False)


async def wait_healthy(request: HealthCheckRequest) -> None:
    """Poll an HTTP endpoint until it responds healthy, or raise on timeout.

    Args:
        request.any_non_5xx: When *False* (default) only ``200`` counts as
            healthy. When *True* any status below 500 is accepted — useful for
            servers that don't expose a dedicated health endpoint.
    """
    start = time.monotonic()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + request.health_timeout_seconds

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=5),
    ) as session:
        while loop.time() < deadline:
            _raise_if_process_exited(request)
            try:
                async with session.get(request.url, headers=request.headers) as resp:
                    healthy = resp.status == 200 or (request.any_non_5xx and resp.status < 500)
            except (aiohttp.ClientError, OSError):
                healthy = False

            if healthy:
                _raise_if_process_exited(request)
                elapsed_ms = (time.monotonic() - start) * 1000
                logger.info(
                    "Health check passed",
                    container=request.container_name,
                    elapsed_ms=round(elapsed_ms),
                )
                return

            if request.process is None and not await is_container_running(request.container_name):
                logs = await run_docker("logs", "--tail", "30", request.container_name, check=False)
                logger.error(
                    "Container exited",
                    container=request.container_name,
                    logs=redacted_container_logs(logs, limit=2000),
                )
                msg = f"Container {request.container_name} failed to start — check logs above"
                raise RuntimeError(msg)

            await asyncio.sleep(request.poll_interval)

    msg = (
        f"Container {request.container_name} did not become healthy within "
        f"{request.health_timeout_seconds}s"
    )
    raise TimeoutError(msg)


def _raise_if_process_exited(request: HealthCheckRequest) -> None:
    if request.process is not None and request.process.poll() is not None:
        msg = f"Script {request.container_name} exited unexpectedly"
        raise RuntimeError(msg)
