"""Shared Docker helpers — subprocess wrappers used by gateway and MCP manager.

Extracted from :mod:`pynchy.container_runner.gateway` so that both
:class:`LiteLLMGateway` and :class:`McpManager` can share them.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import aiohttp

from pynchy.logger import logger


def docker_available() -> bool:
    """Check if ``docker`` is on PATH."""
    return shutil.which("docker") is not None


def run_docker(
    *args: str,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a ``docker`` CLI command."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def ensure_image(image: str) -> None:
    """Pull a Docker image if not already present locally."""
    result = run_docker("image", "inspect", image, check=False)
    if result.returncode == 0:
        return

    logger.info("Pulling Docker image (first run may take a minute)", image=image)
    run_docker("pull", image, timeout=300)
    logger.info("Docker image pulled", image=image)


def ensure_network(name: str) -> None:
    """Create a Docker network if it doesn't already exist."""
    result = run_docker("network", "inspect", name, check=False)
    if result.returncode == 0:
        return
    run_docker("network", "create", name)
    logger.info("Created Docker network", network=name)


def is_container_running(name: str) -> bool:
    """Check if a Docker container is currently running."""
    result = run_docker("inspect", "-f", "{{.State.Running}}", name, check=False)
    return result.stdout.strip() == "true"


async def wait_healthy(
    container_name: str,
    url: str,
    timeout: float = 90,
    poll_interval: float = 1.0,
    headers: dict[str, str] | None = None,
    any_non_5xx: bool = False,
) -> None:
    """Poll an HTTP endpoint until it responds healthy, or raise on timeout.

    Args:
        any_non_5xx: When *False* (default) only ``200`` counts as healthy.
            When *True* any status below 500 is accepted — useful for servers
            that don't expose a dedicated health endpoint.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=5),
    ) as session:
        while loop.time() < deadline:
            try:
                async with session.get(url, headers=headers) as resp:
                    if any_non_5xx and resp.status < 500:
                        return
                    if resp.status == 200:
                        return
            except (aiohttp.ClientError, OSError):
                pass

            if not is_container_running(container_name):
                logs = run_docker("logs", "--tail", "30", container_name, check=False)
                logger.error("Container exited", container=container_name, logs=logs.stdout[-2000:])
                msg = f"Container {container_name} failed to start — check logs above"
                raise RuntimeError(msg)

            await asyncio.sleep(poll_interval)

    msg = f"Container {container_name} did not become healthy within {timeout}s"
    raise TimeoutError(msg)
