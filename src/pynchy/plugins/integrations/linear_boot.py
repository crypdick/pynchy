"""Startup reconciliation for Linear workspace boards."""

from __future__ import annotations

import os
from collections.abc import Iterable

import aiohttp

from pynchy.logger import logger
from pynchy.plugins.integrations.linear import LinearClient
from pynchy.plugins.integrations.linear_boards import (
    LinearWorkspaceBoard,
    create_workspace_todo,
    reconcile_workspace_boards,
)
from pynchy.types import WorkspaceProfile

LINEAR_BOOT_TIMEOUT_SECONDS = 30


async def reconcile_linear_workspace_boards(
    workspaces: Iterable[WorkspaceProfile],
) -> dict[str, LinearWorkspaceBoard]:
    """Create missing Linear projects/states for registered Pynchy workspaces."""
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        return {}

    timeout = aiohttp.ClientTimeout(total=LINEAR_BOOT_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = LinearClient(api_key=api_key, session=session)
        try:
            boards = await reconcile_workspace_boards(
                client,
                list(workspaces),
                team_key=os.environ.get("LINEAR_TEAM_KEY"),
            )
        except Exception as exc:  # allow: exception-handling - Linear is optional at boot
            logger.warning("Linear workspace board reconciliation failed", err=str(exc))
            return {}
    logger.info("Linear workspace boards reconciled", count=len(boards))
    return boards


async def create_linear_workspace_todo(
    workspace: WorkspaceProfile,
    title: str,
) -> dict[str, object] | None:
    """Create one Linear todo issue for a workspace if Linear is configured."""
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        return None

    timeout = aiohttp.ClientTimeout(total=LINEAR_BOOT_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = LinearClient(api_key=api_key, session=session)
        try:
            issue = await create_workspace_todo(
                client,
                workspace,
                title,
                team_key=os.environ.get("LINEAR_TEAM_KEY"),
            )
        except Exception as exc:  # allow: exception-handling - local todo capture still succeeds
            logger.warning(
                "Linear todo creation failed",
                workspace=workspace.folder,
                err=str(exc),
            )
            return None
    return issue
