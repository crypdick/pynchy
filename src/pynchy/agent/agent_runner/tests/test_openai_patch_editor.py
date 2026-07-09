"""Tests for the OpenAI patch editor."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agent_runner.cores.openai import ContainerPatchEditor

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_patch_editor_file_lifecycle(tmp_path: Path) -> None:
    editor = ContainerPatchEditor()

    created_path = tmp_path / "nested" / "file.txt"
    create_result = await editor.create_file(
        SimpleNamespace(path=str(created_path), new_content="hello")
    )
    assert create_result.status == "completed"
    assert created_path.read_text(encoding="utf-8") == "hello"

    update_result = await editor.update_file(
        SimpleNamespace(path=str(created_path), new_content="world")
    )
    assert update_result.status == "completed"
    assert created_path.read_text(encoding="utf-8") == "world"

    delete_result = await editor.delete_file(SimpleNamespace(path=str(created_path)))
    assert delete_result.status == "completed"
    assert not created_path.exists()


@pytest.mark.asyncio
async def test_patch_editor_missing_update_returns_failed(tmp_path: Path) -> None:
    editor = ContainerPatchEditor()

    missing_path = tmp_path / "missing.txt"
    result = await editor.update_file(
        SimpleNamespace(path=str(missing_path), new_content="ignored")
    )

    assert result.status == "failed"
    assert str(missing_path) in (result.output or "")
