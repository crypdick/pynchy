"""Reconcile declarative child conversations below configured workspace roots."""

from __future__ import annotations

from collections.abc import (
    Awaitable,  # noqa: TC003, RUF100 - beartype resolves workspace-thread annotations at runtime.
    Callable,  # noqa: TC003, RUF100 - beartype resolves workspace-thread annotations at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pynchy.config.models import (
    WorkspaceConfig,  # noqa: TC001, RUF100 - beartype resolves workspace-thread annotations at runtime.
)
from pynchy.config.workspace_names import dynamic_thread_folder
from pynchy.host.orchestrator.threads import create_thread, find_thread, supports_thread_lookup
from pynchy.logger import logger
from pynchy.types import Channel, WorkspaceProfile


@dataclass(frozen=True)
class WorkspaceThreadAction:
    """One observable result from declarative child-thread reconciliation."""

    operation: Literal["await_parent", "blocked", "create", "register", "reuse"]
    workspace: str
    thread: str
    jid: str | None = None
    detail: str | None = None


def _child_profile(
    parent: WorkspaceProfile,
    child_jid: str,
    thread_name: str,
    existing: WorkspaceProfile | None,
) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=child_jid,
        name=f"{parent.name}/{thread_name}",
        folder=dynamic_thread_folder(parent.folder, child_jid),
        trigger=parent.trigger,
        container_config=parent.container_config,
        security=parent.security,
        is_admin=parent.is_admin,
        added_at=existing.added_at if existing is not None else datetime.now(UTC).isoformat(),
    )


async def reconcile_workspace_threads(
    workspaces: dict[str, WorkspaceProfile],
    configs: dict[str, WorkspaceConfig],
    channels: list[Channel],
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
    *,
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
                child_jid = await find_thread(channels, parent_jid, declared_thread.name)
            except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; one remote thread must not block startup.
                detail = f"thread lookup failed: {type(exc).__name__}"
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
            if child_jid is None:
                actions.append(WorkspaceThreadAction("create", folder, declared_thread.name))
                if dry_run:
                    continue
                try:
                    child_jid = await create_thread(channels, parent_jid, declared_thread.name)
                except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; one remote thread must not block startup.
                    detail = f"thread creation failed: {type(exc).__name__}"
                    actions.append(
                        WorkspaceThreadAction(
                            "blocked", folder, declared_thread.name, detail=detail
                        )
                    )
                    logger.warning(
                        "Configured workspace thread not reconciled",
                        workspace=folder,
                        thread=declared_thread.name,
                        reason=detail,
                    )
                    continue
            else:
                actions.append(
                    WorkspaceThreadAction("reuse", folder, declared_thread.name, child_jid)
                )

            existing = workspaces.get(child_jid)
            parent = workspaces[parent_jid]
            profile = _child_profile(parent, child_jid, declared_thread.name, existing)
            if existing == profile:
                continue
            actions.append(
                WorkspaceThreadAction("register", folder, declared_thread.name, child_jid)
            )
            if not dry_run:
                await register_fn(profile)
    return actions
