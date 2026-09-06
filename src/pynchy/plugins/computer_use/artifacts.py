"""Shared screenshot artifact projection for computer-use providers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pynchy.host.paths import PYNCHY_IPC_CONTAINER_PATH

_CONTAINER_ARTIFACT_DIR = f"{PYNCHY_IPC_CONTAINER_PATH}/computer-use"


async def screenshot_artifact(path: Path) -> dict[str, Any]:
    """Verify a provider screenshot and expose its attributed IPC mount path."""
    if not await asyncio.to_thread(path.exists):
        raise RuntimeError("computer-use provider did not create the screenshot file")
    stat = await asyncio.to_thread(path.stat)
    return {
        "host_path": str(path),
        "container_path": f"{_CONTAINER_ARTIFACT_DIR}/{path.name}",
        "format": "png",
        "bytes": stat.st_size,
    }
