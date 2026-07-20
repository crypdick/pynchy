"""Workspace naming helpers for Linear projects."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkspaceIdentity(Protocol):
    @property
    def folder(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def jid(self) -> str: ...


def workspace_project_name(workspace: WorkspaceIdentity) -> str:
    name = str(workspace.name or "").strip()
    if name and not _looks_like_repo_slug_display(name):
        return name
    return workspace.folder.replace("-", " ").replace("_", " ").title()


def workspace_marker(workspace: WorkspaceIdentity) -> str:
    return f"pynchy.workspace={workspace.folder}"


def project_description(workspace: WorkspaceIdentity) -> str:
    return f"Managed by Pynchy.\n\n{workspace_marker(workspace)}\npynchy.chat_jid={workspace.jid}"


def todo_description(workspace: WorkspaceIdentity, details: str | None = None) -> str:
    provenance = (
        "Captured from a Pynchy workspace todo.\n\n"
        f"{workspace_marker(workspace)}\n"
        f"pynchy.chat_jid={workspace.jid}"
    )
    normalized_details = str(details or "").strip()
    if not normalized_details:
        return provenance
    return f"{normalized_details}\n\n---\n\n{provenance}"


def project_matches_workspace(project_description: object, workspace: WorkspaceIdentity) -> bool:
    marker = workspace_marker(workspace)
    return marker in {line.strip() for line in str(project_description or "").splitlines()}


def _looks_like_repo_slug_display(name: str) -> bool:
    return "--" in name and name == name.lower()
