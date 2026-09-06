"""Startup reconciliation for Linear workspace boards.

This module sits below the aggregate Linear plugin because webhook routes reuse
its workspace selector. Import provider primitives from leaf modules here to
avoid a plugin -> webhook -> boot import cycle.
"""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
)
from dataclasses import dataclass, field

import aiohttp

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccount,
)
from pynchy.plugins.integrations.linear_boards import (
    LinearWorkspaceBoard,
    WorkspaceTodoProposal,
    create_workspace_todo,
    list_workspace_todos,
    reconcile_workspace_boards,
)
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.workspace.api import (
    WorkspaceProfile,
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


_runtime: LinearBootRuntime | None = None


def configure_linear_boot_runtime(runtime: LinearBootRuntime) -> None:
    """Set resolved Linear boot dependencies before reconciliation."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearBootRuntime:
    if _runtime is None:
        raise RuntimeError("Linear boot runtime has not been configured")
    return _runtime


@dataclass(frozen=True)
class _LinearWorkspaceContext:
    """Canonical workspace identity used for durable Linear boards."""

    folder: str
    name: str
    jid: str
    account: LinearAccount


@dataclass(frozen=True)
class LinearIssueControl:
    """One active Linear issue that needs a silent workspace control."""

    issue_id: str
    workspace: str
    parent_jid: str
    account_name: str
    title: str
    url: str
    updated_at: str


async def reconcile_linear_workspace_boards(
    workspaces: Iterable[WorkspaceProfile],
    ensure_issue_control: Callable[[LinearIssueControl], Awaitable[None]] | None = None,
) -> dict[str, LinearWorkspaceBoard]:
    """Reconcile Linear boards and silently materialize their active issue controls."""
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
                account_boards = await reconcile_workspace_boards(
                    client,
                    account_workspaces,
                    team_key=account.team_key,
                )
                boards.update(account_boards)
                if ensure_issue_control is not None:
                    await _reconcile_issue_controls(
                        client,
                        account_workspaces,
                        ensure_issue_control,
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


async def _reconcile_issue_controls(
    client: LinearClient,
    workspaces: list[_LinearWorkspaceContext],
    ensure_issue_control: Callable[[LinearIssueControl], Awaitable[None]],
) -> None:
    reconciled = 0
    for workspace in workspaces:
        try:
            issues = await list_workspace_todos(
                client,
                workspace,
                team_key=workspace.account.team_key,
            )
        except Exception:  # noqa: BLE001 - one board must not strand other issue controls.
            logger.exception(
                "Linear issue control discovery failed",
                workspace=workspace.folder,
            )
            continue
        for issue in issues:
            control = _issue_control(issue, workspace)
            if control is None:
                logger.warning(
                    "Linear issue control skipped malformed issue",
                    workspace=workspace.folder,
                    issue=issue.get("identifier"),
                )
                continue
            try:
                await ensure_issue_control(control)
                reconciled += 1
            except Exception:  # noqa: BLE001 - one issue must not strand sibling controls.
                logger.exception(
                    "Linear issue control reconciliation failed",
                    workspace=workspace.folder,
                    issue=control.issue_id,
                )
    logger.info("Linear issue controls reconciled", count=reconciled)


def _issue_control(
    issue: dict[str, object],
    workspace: _LinearWorkspaceContext,
) -> LinearIssueControl | None:
    issue_id = issue.get("id")
    identifier = issue.get("identifier")
    title = issue.get("title")
    url = issue.get("url")
    updated_at = issue.get("updatedAt")
    if (
        not isinstance(issue_id, str)
        or not issue_id.strip()
        or not isinstance(identifier, str)
        or not identifier.strip()
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(url, str)
        or not url.strip()
        or not isinstance(updated_at, str)
        or not updated_at.strip()
    ):
        return None
    return LinearIssueControl(
        issue_id=issue_id,
        workspace=workspace.folder,
        parent_jid=workspace.jid,
        account_name=workspace.account.name,
        title=f"[{identifier}] {title}",
        url=url,
        updated_at=updated_at,
    )


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
