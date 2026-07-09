"""Apple Container runtime provider for pynchy."""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404, RUF100 - runtime adapter uses fixed no-shell container CLI argv.


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
            except Exception as exc:
                raise RuntimeError(
                    "Apple Container system is required but failed to start"
                ) from exc

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        result = subprocess.run(  # noqa: S603, RUF100 - runtime CLI is fixed by this adapter and argv is trusted.
            [self.cli, "ls", "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        import json

        containers = json.loads(result.stdout or "[]")
        names: list[str] = []
        for c in containers:
            status = c.get("status")
            state = status.get("state") if isinstance(status, dict) else status
            name = c.get("configuration", {}).get("id", "")
            if state == "running" and name.startswith(prefix):
                names.append(name)
        return names
