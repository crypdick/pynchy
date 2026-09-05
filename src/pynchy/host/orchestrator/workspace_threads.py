"""Reconcile declarative child conversations below configured workspace roots."""

from __future__ import annotations

from collections.abc import (
    Awaitable,  # noqa: TC003 - beartype resolves workspace-thread annotations at runtime.
    Callable,  # noqa: TC003 - beartype resolves workspace-thread annotations at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast

from pynchy.conversation.api import dynamic_thread_folder
from pynchy.host.orchestrator.threads import ensure_thread, supports_thread_lookup
from pynchy.host.orchestrator.workspace_registration import workspace_security
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.workspace.api import ResolvedWorkspaceConfig, WorkspaceProfile

type WorkspaceConfig = Any
type WorkspaceThreadConfig = Any


def _unconfigured_settings() -> NoReturn:
    raise RuntimeError("Workspace thread configuration has not been composed")


get_settings: Callable[[], Any] = _unconfigured_settings


def configure_workspace_threads_runtime(*, settings: Callable[[], Any]) -> None:
    """Bind child-policy settings at host composition."""
    global get_settings  # noqa: PLW0603 - one host process owns workspace thread policy.
    get_settings = settings


@dataclass(frozen=True)
class WorkspaceThreadAction:
    """One observable result from declarative child-thread reconciliation."""

    operation: Literal["await_parent", "blocked", "create", "register", "reuse"]
    workspace: str
    thread: str
    jid: str | None = None
    detail: str | None = None


def _child_profile(  # noqa: PLR0913 - profile construction keeps policy and placement explicit.
    parent: WorkspaceProfile,
    child_jid: str,
    thread_name: str,
    folder: str,
    resolved: ResolvedWorkspaceConfig,
    existing: WorkspaceProfile | None,
) -> WorkspaceProfile:
    dynamic_folder = dynamic_thread_folder(parent.folder, child_jid)
    return WorkspaceProfile(
        jid=child_jid,
        name=(
            f"{parent.name}/{thread_name}"
            if folder == dynamic_folder
            else folder.replace("-", " ").title()
        ),
        folder=folder,
        trigger=parent.trigger,
        container_config=parent.container_config,
        security=workspace_security(resolved),
        is_admin=resolved.is_admin,
        added_at=existing.added_at if existing is not None else datetime.now(UTC).isoformat(),
    )


def _declared_child_profile(
    parent: WorkspaceProfile,
    child_jid: str,
    declared_thread: WorkspaceThreadConfig,
    existing: WorkspaceProfile | None,
) -> WorkspaceProfile:
    """Build either an inherited category child or a semantic policy owner."""
    if declared_thread.workspace is None:
        return WorkspaceProfile(
            jid=child_jid,
            name=f"{parent.name}/{declared_thread.name}",
            folder=dynamic_thread_folder(parent.folder, child_jid),
            trigger=parent.trigger,
            container_config=parent.container_config,
            security=parent.security,
            is_admin=parent.is_admin,
            added_at=(existing.added_at if existing is not None else datetime.now(UTC).isoformat()),
        )

    settings = get_settings()
    child_folder = declared_thread.workspace
    resolved = settings.resolved_workspace_config(child_folder)
    if resolved is None:
        raise RuntimeError(f"Declared workspace thread lacks policy: {child_folder}")
    return _child_profile(
        parent,
        child_jid,
        declared_thread.name,
        child_folder,
        resolved,
        existing,
    )


def _child_registration_fn(
    parent: WorkspaceProfile,
    declared_thread: WorkspaceThreadConfig,
    existing: WorkspaceProfile | None,
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
    rebind_fn: Callable[[WorkspaceProfile], Awaitable[None]] | None,
) -> Callable[[WorkspaceProfile], Awaitable[None]]:
    if (
        rebind_fn is not None
        and declared_thread.workspace is not None
        and existing is not None
        and existing.folder == dynamic_thread_folder(parent.folder, existing.jid)
    ):
        return rebind_fn
    return register_fn


async def reconcile_workspace_threads(  # noqa: PLR0913 - registration and optional rebind are distinct persistence operations.
    workspaces: dict[str, WorkspaceProfile],
    configs: dict[str, WorkspaceConfig],
    channels: list[Channel],
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
    *,
    rebind_fn: Callable[[WorkspaceProfile], Awaitable[None]] | None = None,
    dry_run: bool = False,
) -> list[WorkspaceThreadAction]:
    """Ensure configured child threads exist and inherit their parent workspace.

    Creation only proceeds when the owning channel can find an existing thread.
    That lookup requirement prevents a restart from silently creating duplicate
    child conversations on channels that lack idempotent lookup support.
    """
    actions: list[WorkspaceThreadAction] = []
    folder_to_jid = {profile.folder: jid for jid, profile in workspaces.items()}
    for folder, config in configs.items():
        parent_jid = folder_to_jid.get(folder)
        for declared_thread in config.threads:
            if parent_jid is None:
                actions.append(WorkspaceThreadAction("await_parent", folder, declared_thread.name))
                continue
            if not supports_thread_lookup(channels, parent_jid):
                detail = "owning channel cannot look up child threads"
                actions.append(
                    WorkspaceThreadAction("blocked", folder, declared_thread.name, detail=detail)
                )
                logger.warning(
                    "Configured workspace thread not reconciled",
                    workspace=folder,
                    thread=declared_thread.name,
                    reason=detail,
                )
                continue

            try:
                ensured = await ensure_thread(
                    channels,
                    parent_jid,
                    declared_thread.name,
                    kind=declared_thread.kind,
                    dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001 - allow: exception-handling; one remote thread must not block startup.
                detail = f"thread ensure failed: {type(exc).__name__}"
                actions.append(
                    WorkspaceThreadAction("blocked", folder, declared_thread.name, detail=detail)
                )
                logger.warning(
                    "Configured workspace thread not reconciled",
                    workspace=folder,
                    thread=declared_thread.name,
                    reason=detail,
                )
                continue
            if ensured.created:
                actions.append(WorkspaceThreadAction("create", folder, declared_thread.name))
                if dry_run:
                    continue
            else:
                actions.append(
                    WorkspaceThreadAction("reuse", folder, declared_thread.name, ensured.jid)
                )

            child_jid = cast("str", ensured.jid)
            existing = workspaces.get(child_jid)
            parent = workspaces[parent_jid]
            profile = _declared_child_profile(
                parent,
                child_jid,
                declared_thread,
                existing,
            )
            if existing == profile:
                continue
            if dry_run:
                actions.append(
                    WorkspaceThreadAction("register", folder, declared_thread.name, child_jid)
                )
                continue
            try:
                update_fn = _child_registration_fn(
                    parent,
                    declared_thread,
                    existing,
                    register_fn,
                    rebind_fn,
                )
                await update_fn(profile)
            except Exception as exc:  # noqa: BLE001 - allow: exception-handling; one conflicting child must not block startup.
                detail = f"workspace registration failed: {type(exc).__name__}"
                actions.append(
                    WorkspaceThreadAction(
                        "blocked", folder, declared_thread.name, child_jid, detail
                    )
                )
                logger.warning(
                    "Configured workspace thread not reconciled",
                    workspace=folder,
                    thread=declared_thread.name,
                    jid=child_jid,
                    reason=detail,
                )
                continue
            actions.append(
                WorkspaceThreadAction("register", folder, declared_thread.name, child_jid)
            )
    return actions
