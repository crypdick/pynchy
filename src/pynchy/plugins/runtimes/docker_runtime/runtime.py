"""Docker container runtime provider for pynchy."""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404, RUF100 - runtime adapter uses fixed no-shell Docker/open argv.
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
            subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
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
        result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
            [self.cli, "ps", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            check=False,
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
                ["open", "-a", "Docker"],  # noqa: S607, RUF100 - macOS open is a trusted platform launcher and argv is fixed.
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
                    ["docker", "info"],  # noqa: S607, RUF100 - docker is the trusted runtime CLI and argv is fixed.
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

        raise RuntimeError(
            "Docker Desktop was launched but the daemon did not become ready "
            "within 60s. Check Docker Desktop for errors."
        ) from original_exc
