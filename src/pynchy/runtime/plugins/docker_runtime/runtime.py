"""Docker container runtime provider for pynchy."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

from pynchy.logger import logger


class DockerContainerRuntime:
    """Runtime adapter for the Docker CLI."""

    name = "docker"
    cli = "docker"

    def is_available(self) -> bool:
        return shutil.which(self.cli) is not None

    def ensure_running(self) -> None:
        try:
            subprocess.run(
                [self.cli, "info"],
                capture_output=True,
                check=True,
            )
            logger.debug("Docker daemon is running")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            if sys.platform == "darwin":
                self._start_docker_desktop(exc)
            else:
                raise RuntimeError(
                    "Docker is required but not running. Start with: sudo systemctl start docker"
                ) from exc

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        result = subprocess.run(
            [self.cli, "ps", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
        )
        names: list[str] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            c = json.loads(line)
            name = c.get("Names", "")
            if name.startswith(prefix):
                names.append(name)
        return names

    # ------------------------------------------------------------------

    @staticmethod
    def _start_docker_desktop(original_exc: Exception) -> None:
        """Attempt to launch Docker Desktop on macOS and wait for the daemon."""
        logger.info("Docker not running, attempting to start Docker Desktop...")
        try:
            subprocess.run(
                ["open", "-a", "Docker"],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise RuntimeError(
                "Docker Desktop is required but could not be started. "
                "Install from https://www.docker.com/products/docker-desktop/"
            ) from exc

        for i in range(30):
            try:
                subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    check=True,
                )
                logger.info("Docker Desktop started successfully")
                return
            except (subprocess.CalledProcessError, FileNotFoundError):
                if i % 5 == 0:
                    logger.info("Waiting for Docker Desktop to start...")
                time.sleep(2)

        raise RuntimeError(
            "Docker Desktop was launched but the daemon did not become ready "
            "within 60s. Check Docker Desktop for errors."
        ) from original_exc
