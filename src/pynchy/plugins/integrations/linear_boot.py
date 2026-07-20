"""Startup reconciliation for Linear workspace boards.

This module sits below the aggregate Linear plugin because webhook routes reuse
its workspace selector. Import provider primitives from leaf modules here to
avoid a plugin -> webhook -> boot import cycle.
"""

from __future__ import annotations

from collections.abc import (
    Iterable,  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
)
from dataclasses import dataclass

import aiohttp

from pynchy.config import get_settings
from pynchy.host.orchestrator.workspace_config import static_workspace_folder
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccount,
    linear_account_for_workspace,
)
from pynchy.plugins.integrations.linear_boards import (
    LinearWorkspaceBoard,
    WorkspaceTodoProposal,
    create_workspace_todo,
    reconcile_workspace_boards,
)
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_statuses import READY_FOR_PLANNING_STATUS
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
    WorkspaceProfile,
)

LINEAR_BOOT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class _LinearWorkspaceContext:
    """Canonical workspace identity used for durable Linear boards."""

    folder: str
    name: str
    jid: str
    account: LinearAccount


async def reconcile_linear_workspace_boards(
    workspaces: Iterable[WorkspaceProfile],
) -> dict[str, LinearWorkspaceBoard]:
    """Create missing Linear projects/states for registered Pynchy workspaces."""
    selected_workspaces = _linear_workspaces(workspaces)
    if not selected_workspaces:
        return {}
    boards: dict[str, LinearWorkspaceBoard] = {}
    for account_name in dict.fromkeys(context.account.name for context in selected_workspaces):
        account_workspaces = [
            context for context in selected_workspaces if context.account.name == account_name
        ]
        account = account_workspaces[0].account
        api_key = account.api_key
        if not api_key:
            continue
        timeout = aiohttp.ClientTimeout(total=LINEAR_BOOT_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            client = LinearClient(api_key=api_key, session=session, team_key=account.team_key)
            try:
                boards.update(
                    await reconcile_workspace_boards(
                        client,
                        account_workspaces,
                        team_key=account.team_key,
                    )
                )
            except Exception as exc:  # noqa: BLE001, RUF100 - one optional account must not block startup.
                logger.warning(
                    "Linear workspace board reconciliation failed",
                    account=account_name,
                    err=str(exc),
                )
    logger.info("Linear workspace boards reconciled", count=len(boards))
    return boards


def _linear_workspaces(workspaces: Iterable[WorkspaceProfile]) -> list[_LinearWorkspaceContext]:
    result: list[_LinearWorkspaceContext] = []
    seen_folders: set[str] = set()
    for workspace in workspaces:
        context = _linear_workspace_context(workspace)
        if context is None or context.folder in seen_folders:
            continue
        seen_folders.add(context.folder)
        result.append(context)
    return result


def _linear_workspace_context(workspace: WorkspaceProfile) -> _LinearWorkspaceContext | None:
    settings = get_settings()
    folder = static_workspace_folder(workspace.folder)
    resolved = settings.resolved_workspace_config(folder)
    if resolved is None:
        return None
    account = linear_account_for_workspace(folder, settings)
    if account is None:
        return None

    name = workspace.name if folder == workspace.folder else folder.replace("-", " ").title()
    return _LinearWorkspaceContext(
        folder=folder,
        name=name,
        jid=workspace.jid,
        account=account,
    )


def linear_workspace_enabled(workspace: WorkspaceProfile) -> bool:
    """Return whether this workspace selected Linear as its canonical todo board."""
    return _linear_workspace_context(workspace) is not None


async def create_linear_workspace_todo(
    workspace: WorkspaceProfile,
    title: str,
) -> dict[str, object] | None:
    """Create one Linear todo issue for a workspace if Linear is configured."""
    context = _linear_workspace_context(workspace)
    if context is None:
        return None

    api_key = context.account.api_key
    if not api_key:
        return None

    timeout = aiohttp.ClientTimeout(total=LINEAR_BOOT_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = LinearClient(
            api_key=api_key,
            session=session,
            team_key=context.account.team_key,
        )
        try:
            issue = await create_workspace_todo(
                client,
                context,
                WorkspaceTodoProposal(title=title),
                team_key=context.account.team_key,
                status=READY_FOR_PLANNING_STATUS,
            )
        except Exception as exc:  # noqa: BLE001, RUF100 - local todo capture still succeeds even if Linear fails.
            logger.warning(
                "Linear todo creation failed",
                workspace=context.folder,
                err=str(exc),
            )
            return None
    return issue
