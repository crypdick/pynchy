"""Workspace layout prompt-scope validation contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config.workspace_layout import WorkspaceScopeConfig, WorkspaceThreadConfig


@pytest.mark.parametrize(
    "factory",
    [
        lambda: WorkspaceThreadConfig(soul="prompts/default"),
        lambda: WorkspaceScopeConfig(
            workspace="child", profiles=["worker"], soul="prompts/default"
        ),
    ],
)
def test_workspace_layout_souls_must_use_souls_scope(factory) -> None:
    with pytest.raises(ValidationError, match="workspace soul must use the souls/ scope"):
        factory()
