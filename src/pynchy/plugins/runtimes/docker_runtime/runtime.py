"""Docker container runtime provider for pynchy."""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 - runtime adapter uses fixed no-shell Docker/open argv.
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.container_labels import (
    AGENT_CONTAINER_LABEL,
    AGENT_CONTAINER_LABEL_VALUE,
)
from pynchy.logger import logger

_DOCKER_REQUIRED_BUT_NOT_RUNNING = (
    "Docker is required but not running. Start with: sudo systemctl start docker"
)
_DOCKER_DESKTOP_REQUIRED_BUT_COULD_NOT_BE_STARTED = (
    "Docker Desktop is required but could not be started. "
    "Install from https://www.docker.com/products/docker-desktop/"
)
_DOCKER_DESKTOP_DID_NOT_BECOME_READY = (
    "Docker Desktop was launched but the daemon did not become ready "
    "within 60s. Check Docker Desktop for errors."
)


@dataclass(frozen=True)
class RuntimeContainer:
    """Container record from Docker."""

    name: str
    state: str
    image: str
    created_at: datetime | None
    labels: dict[str, str]

    @property
    def is_agent_container(self) -> bool:
        return self.labels.get(AGENT_CONTAINER_LABEL) == AGENT_CONTAINER_LABEL_VALUE


def _parse_created_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(value, fmt).astimezone(UTC)
        except ValueError:
            continue
    return None


def _parse_labels(value: object) -> dict[str, str]:
    if not isinstance(value, str) or not value:
        return {}
    result: dict[str, str] = {}
    for item in value.split(","):
        key, sep, raw_value = item.partition("=")
        if sep and key:
            result[key] = raw_value
    return result


class DockerContainerRuntime:
    """Runtime adapter for the Docker CLI."""

    name = "docker"
    cli = "docker"

    def is_available(self) -> bool:
        return shutil.which(self.cli) is not None

    def ensure_running(self) -> None:
        try:
            subprocess.run(  # noqa: S603 - runtime CLI is fixed by this adapter and argv is trusted.
                [self.cli, "info"],
                capture_output=True,
                check=True,
            )
            logger.debug("Docker daemon is running")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            if sys.platform == "darwin":
                self._start_docker_desktop(exc)
            else:
                raise RuntimeError(_DOCKER_REQUIRED_BUT_NOT_RUNNING) from exc

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        return [
            container.name
            for container in self.list_containers(prefix=prefix)
            if container.state == "running"
        ]

    # ------------------------------------------------------------------

    @staticmethod
    def _start_docker_desktop(original_exc: Exception) -> None:
        """Attempt to launch Docker Desktop on macOS and wait for the daemon."""
        logger.info("Docker not running, attempting to start Docker Desktop...")
        try:
            subprocess.run(
                ["open", "-a", "Docker"],  # noqa: S607 - macOS open is a trusted platform launcher and argv is fixed.
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise RuntimeError(_DOCKER_DESKTOP_REQUIRED_BUT_COULD_NOT_BE_STARTED) from exc

        for i in range(30):
            try:
                subprocess.run(
                    ["docker", "info"],  # noqa: S607 - docker is the trusted runtime CLI and argv is fixed.
                    capture_output=True,
                    check=True,
                )
                logger.info("Docker Desktop started successfully")
            except (subprocess.CalledProcessError, FileNotFoundError):
                if i % 5 == 0:
                    logger.info("Waiting for Docker Desktop to start...")
                time.sleep(2)
            else:
                return

        raise RuntimeError(_DOCKER_DESKTOP_DID_NOT_BECOME_READY) from original_exc

    def list_containers(self, prefix: str = "pynchy-") -> list[RuntimeContainer]:
        result = subprocess.run(  # noqa: S603 - runtime CLI is fixed by this adapter and argv is trusted.
            [self.cli, "ps", "-a", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        containers: list[RuntimeContainer] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            item = json.loads(line)
            name = item.get("Names", "")
            if not isinstance(name, str) or not name.startswith(prefix):
                continue
            state = item.get("State", "")
            image = item.get("Image", "")
            containers.append(
                RuntimeContainer(
                    name=name,
                    state=state if isinstance(state, str) else "",
                    image=image if isinstance(image, str) else "",
                    created_at=_parse_created_at(item.get("CreatedAt")),
                    labels=_parse_labels(item.get("Labels")),
                )
            )
        return containers

    def remove_container(self, name: str, *, force: bool = True) -> bool:
        args = [self.cli, "rm"]
        if force:
            args.append("-f")
        args.append(name)
        result = subprocess.run(  # noqa: S603 - runtime CLI is fixed by this adapter and argv is trusted.
            args,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def prune_images(self, *, all_images: bool = False) -> bool:
        """Prune dangling images, or every unreferenced image when requested."""
        args = [self.cli, "image", "prune", "-f"]
        if all_images:
            args.append("-a")
        result = subprocess.run(  # noqa: S603 - runtime CLI is fixed by this adapter and argv is trusted.
            args,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        return result.returncode == 0
