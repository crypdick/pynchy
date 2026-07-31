"""Startup reconciliation for Linear workspace boards.

This module sits below the aggregate Linear plugin because webhook routes reuse
its workspace selector. Import provider primitives from leaf modules here to
avoid a plugin -> webhook -> boot import cycle.
"""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves Linear boot callbacks at runtime.
    Iterable,  # noqa: TC003 - beartype resolves this runtime annotation.
)
from dataclasses import dataclass, field

import aiohttp

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccount,  # noqa: TC001 - beartype resolves Linear boot runtime callbacks at runtime.
)
from pynchy.plugins.integrations.linear_boards import (
    LinearWorkspaceBoard,
    WorkspaceTodoProposal,
    create_workspace_todo,
    reconcile_workspace_boards,
)
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves this runtime annotation.
)

LINEAR_BOOT_TIMEOUT_SECONDS = 30


@dataclass
class _LinearBoardRegistry:
    boards: dict[str, LinearWorkspaceBoard] = field(default_factory=dict)
    configured_workspaces: frozenset[str] = frozenset()
    routable_workspaces: frozenset[str] = frozenset()


_registry = _LinearBoardRegistry()


@dataclass(frozen=True)
class LinearBootRuntime:
    """Resolved workspace and account policy required for Linear board boot."""

    workspace_names: tuple[str, ...]
    account_for_name: Callable[[str], LinearAccount]
    account_for_workspace: Callable[[str], LinearAccount | None]
    workspace_parent: Callable[[str], str | None]
    canonical_workspace_folder: Callable[[str], str]
    additional_workspaces: Callable[[list[WorkspaceProfile]], tuple[WorkspaceProfile, ...]]


@dataclass
class _RuntimeState:
    runtime: LinearBootRuntime | None = None


_runtime = _RuntimeState()


def configure_linear_boot_runtime(runtime: LinearBootRuntime) -> None:
    """Set resolved Linear boot dependencies before reconciliation."""
    _runtime.runtime = runtime


def _configured_runtime() -> LinearBootRuntime:
    if _runtime.runtime is None:
        raise RuntimeError("Linear boot runtime has not been configured")
    return _runtime.runtime


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
    _registry.configured_workspaces = frozenset(_configured_runtime().workspace_names)
    _registry.routable_workspaces = frozenset(
        workspace.folder
        for workspace in selected_workspaces
        if workspace.jid.startswith("discord:channel:")
    )
    if not selected_workspaces:
        _registry.boards = {}
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
            except Exception as exc:  # noqa: BLE001 - one optional account must not block startup.
                logger.warning(
                    "Linear workspace board reconciliation failed",
                    account=account_name,
                    err=str(exc),
                )
    logger.info("Linear workspace boards reconciled", count=len(boards))
    _registry.boards = dict(boards)
    return boards


def configured_linear_workspace_names(account_name: str) -> tuple[str, ...]:
    """Return thread-capable policy owners for one exact Linear account."""
    runtime = _configured_runtime()
    account = runtime.account_for_name(account_name)
    result: list[str] = []
    for workspace in runtime.workspace_names:
        workspace_account = runtime.account_for_workspace(workspace)
        if workspace_account is not None and workspace_account.name == account.name:
            result.append(workspace)
    configured = frozenset(runtime.workspace_names)
    if configured == _registry.configured_workspaces:
        return tuple(
            workspace for workspace in result if workspace in _registry.routable_workspaces
        )
    return tuple(
        workspace for workspace in result if runtime.workspace_parent(workspace) is not None
    )


def workspace_for_linear_project(project_id: str) -> str | None:
    """Resolve one provider project ID to its exact workspace policy owner."""
    for workspace, board in _registry.boards.items():
        if board.project.get("id") == project_id:
            return workspace
    return None


def linear_workspace_boards() -> dict[str, LinearWorkspaceBoard]:
    """Return the managed board identities established during startup."""
    return dict(_registry.boards)


def _linear_workspaces(workspaces: Iterable[WorkspaceProfile]) -> list[_LinearWorkspaceContext]:
    registered = list(workspaces)
    candidates = [*registered, *_configured_runtime().additional_workspaces(registered)]
    result: dict[str, _LinearWorkspaceContext] = {}
    for workspace in candidates:
        context = _linear_workspace_context(workspace)
        if context is None:
            continue
        if context.folder not in result or workspace.folder == context.folder:
            result[context.folder] = context
    return list(result.values())


def _linear_workspace_context(workspace: WorkspaceProfile) -> _LinearWorkspaceContext | None:
    runtime = _configured_runtime()
    folder = runtime.canonical_workspace_folder(workspace.folder)
    account = runtime.account_for_workspace(folder)
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
            )
        except Exception as exc:  # noqa: BLE001 - local todo capture still succeeds even if Linear fails.
            logger.warning(
                "Linear todo creation failed",
                workspace=context.folder,
                err=str(exc),
            )
            return None
    return issue
