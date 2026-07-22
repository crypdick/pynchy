"""Apple Container runtime provider for pynchy."""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404, RUF100 - runtime adapter uses fixed no-shell container CLI argv.
from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.host.container_manager.labels import (
    AGENT_CONTAINER_LABEL,
    AGENT_CONTAINER_LABEL_VALUE,
)

_APPLE_CONTAINER_START_FAILURE_MESSAGE = "Apple Container system is required but failed to start"


@dataclass(frozen=True)
class RuntimeContainer:
    """Container record from Apple Container."""

    name: str
    state: str
    image: str
    created_at: datetime | None
    labels: dict[str, str]

    @property
    def is_agent_container(self) -> bool:
        if self.labels.get(AGENT_CONTAINER_LABEL) == AGENT_CONTAINER_LABEL_VALUE:
            return True
        return self.name.startswith("pynchy-") and self.image.startswith("pynchy-agent:")


def _parse_created_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _container_name(item: dict[str, object]) -> str:
    config = item.get("configuration")
    if not isinstance(config, dict):
        return ""
    name = config.get("id")
    return name if isinstance(name, str) else ""


def _container_state(item: dict[str, object]) -> str:
    status = item.get("status")
    if isinstance(status, dict):
        state = status.get("state")
        return state if isinstance(state, str) else ""
    return status if isinstance(status, str) else ""


def _container_image(item: dict[str, object]) -> str:
    config = item.get("configuration")
    if not isinstance(config, dict):
        return ""
    image = config.get("image")
    if isinstance(image, dict):
        reference = image.get("reference")
        return reference if isinstance(reference, str) else ""
    return image if isinstance(image, str) else ""


def _container_labels(item: dict[str, object]) -> dict[str, str]:
    config = item.get("configuration")
    if not isinstance(config, dict):
        return {}
    raw = config.get("labels")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


class AppleContainerRuntime:
    """Runtime adapter for Apple's ``container`` CLI."""

    name = "apple"
    cli = "container"

    def is_available(self) -> bool:
        return shutil.which(self.cli) is not None

    def ensure_running(self) -> None:
        try:
            subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
                [self.cli, "system", "status"],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            try:
                subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
                    [self.cli, "system", "start"],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                raise RuntimeError(_APPLE_CONTAINER_START_FAILURE_MESSAGE) from exc

    def list_containers(self, prefix: str = "pynchy-") -> list[RuntimeContainer]:
        result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
            [self.cli, "ls", "--all", "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        containers = json.loads(result.stdout or "[]")
        records: list[RuntimeContainer] = []
        for item in containers:
            if not isinstance(item, dict):
                continue
            name = _container_name(item)
            if not name.startswith(prefix):
                continue
            config = item.get("configuration")
            created_at = config.get("creationDate") if isinstance(config, dict) else None
            records.append(
                RuntimeContainer(
                    name=name,
                    state=_container_state(item),
                    image=_container_image(item),
                    created_at=_parse_created_at(created_at),
                    labels=_container_labels(item),
                )
            )
        return records

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        return [
            container.name
            for container in self.list_containers(prefix=prefix)
            if container.state == "running"
        ]

    def remove_container(self, name: str, *, force: bool = True) -> bool:
        args = [self.cli, "rm"]
        if force:
            args.append("--force")
        args.append(name)
        result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
            args,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def cleanup_builder(self) -> bool:
        """Remove Apple Container's BuildKit container after Pynchy image builds."""
        removed = False
        for args in (("builder", "stop"), ("builder", "rm", "--force")):
            result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
                [self.cli, *args],
                capture_output=True,
                text=True,
                check=False,
            )
            if args[1] == "rm":
                removed = result.returncode == 0
        return removed

    def prune_images(self, *, all_images: bool = False) -> bool:
        """Prune dangling images, or every unreferenced image when requested."""
        args = [self.cli, "image", "prune"]
        if all_images:
            args.append("--all")
        result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
            args,
            capture_output=True,
            text=True,
            input="",
            timeout=300,
            check=False,
        )
        return result.returncode == 0
