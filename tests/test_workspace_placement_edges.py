"""Fail-closed workspace placement contracts."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pynchy.host.orchestrator.api import resolve_workspace_placement
from pynchy.host.orchestrator.app import PynchyApp
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


def test_placement_uses_resolved_owner_profile_when_missing() -> None:
    owner = _workspace("owner")
    with (
        patch(
            "pynchy.host.orchestrator.workspace_placement._workspace_parent",
            side_effect=lambda _folder: "root",
        ),
        patch(
            "pynchy.host.orchestrator.workspace_placement._missing_workspace_profile",
            side_effect=lambda _folder, _parent: owner,
        ),
    ):
        placement = resolve_workspace_placement([_workspace("root")], "owner")

    assert placement is not None
    assert placement.owner is owner
    assert placement.control_parent.folder == "root"


def test_placement_uses_existing_owner_profile() -> None:
    owner = _workspace("owner")
    with patch(
        "pynchy.host.orchestrator.workspace_placement._workspace_parent",
        side_effect=lambda _folder: "root",
    ):
        placement = resolve_workspace_placement([_workspace("root"), owner], "owner")

    assert placement is not None
    assert placement.owner is owner
    assert placement.control_parent.folder == "root"


def test_application_composition_fails_closed_for_unknown_workspace_profile() -> None:
    PynchyApp()

    assert resolve_workspace_placement([_workspace("root")], "missing") is None
