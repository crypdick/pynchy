"""Tests for the OpenAI patch editor."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agents.editor import ApplyPatchOperation

from agent_runner.cores.openai import ContainerPatchEditor
from agent_runner.hooks import HookDecision

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_patch_editor_file_lifecycle(tmp_path: Path) -> None:
    editor = ContainerPatchEditor()

    created_path = tmp_path / "nested" / "file.txt"
    create_result = await editor.create_file(
        ApplyPatchOperation(type="create_file", path=str(created_path), diff="+hello")
    )
    assert create_result.status == "completed"
    assert created_path.read_text(encoding="utf-8") == "hello"

    update_result = await editor.update_file(
        ApplyPatchOperation(type="update_file", path=str(created_path), diff="@@\n-hello\n+world")
    )
    assert update_result.status == "completed"
    assert created_path.read_text(encoding="utf-8") == "world"

    delete_result = await editor.delete_file(
        ApplyPatchOperation(type="delete_file", path=str(created_path))
    )
    assert delete_result.status == "completed"
    assert not created_path.exists()


@pytest.mark.asyncio
async def test_patch_editor_resolves_relative_paths_from_agent_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_root = tmp_path / "workspace"
    repo_root = tmp_path / "repo"
    process_root.mkdir()
    repo_root.mkdir()
    monkeypatch.chdir(process_root)
    editor = ContainerPatchEditor(cwd=str(repo_root))

    result = await editor.create_file(
        ApplyPatchOperation(type="create_file", path="src/example.py", diff="+content")
    )

    assert result.status == "completed"
    assert (repo_root / "src/example.py").read_text(encoding="utf-8") == "content"
    assert not (process_root / "src/example.py").exists()


@pytest.mark.asyncio
async def test_patch_editor_missing_update_returns_failed(tmp_path: Path) -> None:
    editor = ContainerPatchEditor()

    missing_path = tmp_path / "missing.txt"
    result = await editor.update_file(
        ApplyPatchOperation(type="update_file", path=str(missing_path), diff="+ignored")
    )

    assert result.status == "failed"
    assert str(missing_path) in (result.output or "")


@pytest.mark.asyncio
async def test_patch_editor_runs_shared_security_roster_before_write(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def deny_persistence(  # noqa: RUF029 - hook protocol is asynchronous.
        tool_name: str, tool_input: dict[str, object]
    ) -> HookDecision:
        calls.append((tool_name, tool_input))
        return HookDecision(allowed=False, reason="PERSIST001")

    editor = ContainerPatchEditor([deny_persistence])
    target = tmp_path / ".bashrc"
    result = await editor.create_file(
        ApplyPatchOperation(type="create_file", path=str(target), diff="+payload")
    )

    assert result.status == "failed"
    assert result.output is not None
    assert "PERSIST001" in result.output
    assert calls == [("apply_patch", {"path": str(target), "diff": "+payload"})]
    assert not target.exists()
