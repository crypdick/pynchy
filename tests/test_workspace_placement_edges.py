"""Fail-closed workspace placement contracts."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pynchy.host.orchestrator.api import resolve_workspace_placement
from pynchy.workspace.api import WorkspaceProfile


def _workspace(folder: str) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=f"{folder}@g.us",
        name=folder,
        folder=folder,
        trigger="@pynchy",
    )


def test_placement_requires_composition_configuration() -> None:
    with (
        patch("pynchy.host.orchestrator.workspace_placement._workspace_parent", None),
        patch("pynchy.host.orchestrator.workspace_placement._missing_workspace_profile", None),
        pytest.raises(RuntimeError, match="has not been configured"),
    ):
        resolve_workspace_placement([_workspace("owner")], "owner")


def test_placement_returns_none_when_control_parent_is_missing() -> None:
    with (
        patch(
            "pynchy.host.orchestrator.workspace_placement._workspace_parent",
            side_effect=lambda _folder: "missing",
        ),
        patch(
            "pynchy.host.orchestrator.workspace_placement._missing_workspace_profile",
            side_effect=lambda _folder, _parent: None,
        ),
    ):
        assert resolve_workspace_placement([_workspace("owner")], "owner") is None


def test_placement_returns_none_when_owner_profile_cannot_be_resolved() -> None:
    with (
        patch(
            "pynchy.host.orchestrator.workspace_placement._workspace_parent",
            side_effect=lambda _folder: "root",
        ),
        patch(
            "pynchy.host.orchestrator.workspace_placement._missing_workspace_profile",
            side_effect=lambda _folder, _parent: None,
        ),
    ):
        assert resolve_workspace_placement([_workspace("root")], "owner") is None
