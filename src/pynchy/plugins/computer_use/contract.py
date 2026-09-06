"""Provider protocol for the backend-neutral computer-use tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pynchy.plugins.computer_use.models import (
    ComputerUseRequest,
)


@dataclass(frozen=True, kw_only=True)
class ComputerUseBackendAvailability:
    """Read-only provider availability evidence used for routing and status."""

    available: bool
    reason: str | None = None


@runtime_checkable
class ComputerUseBackend(Protocol):
    """One platform automation provider contributed by a plugin."""

    @property
    def name(self) -> str:
        """Return the stable provider identifier used in computer-use configuration."""

    def availability(self) -> ComputerUseBackendAvailability:
        """Inspect local prerequisites without mutating host state."""

    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        """Execute an already parsed request through this provider."""
