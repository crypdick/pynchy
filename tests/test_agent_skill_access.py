"""Tests for Pynchy's in-session learned-skill discovery and access flow."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner.agent_tools import call_tool


def _write_skill(root: Path, name: str, description: str = "Useful vault workflow.") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"
    )


def _response(payload: dict[str, object]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload))]


@pytest.mark.asyncio
@pytest.mark.action("skill.catalog.search")
async def test_search_skills_returns_matching_catalog_entries(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "obsidian-knowledge", "Read and search the Obsidian vault.")
    _write_skill(root, "pynchy-operations", "Safely operate the Pynchy service.")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    monkeypatch.delenv("PYNCHY_PROFILE_SKILLS_ROOT", raising=False)

    result = await call_tool("search_skills", {"query": "obsidian search"})

    assert "obsidian-knowledge" in result[0].text
    assert "request_skill_access" in result[0].text


@pytest.mark.asyncio
@pytest.mark.action("skill.catalog.search")
async def test_search_skills_ignores_legacy_profile_catalog(tmp_path: Path, monkeypatch) -> None:
    global_root = tmp_path / "global-skills"
    legacy_profile_root = tmp_path / "profile-skills"
    _write_skill(global_root, "shared-workflow", "Shared workflow.")
    _write_skill(legacy_profile_root, "legacy-workflow", "Legacy profile workflow.")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(global_root))
    monkeypatch.setenv("PYNCHY_PROFILE_SKILLS_ROOT", str(legacy_profile_root))

    result = await call_tool("search_skills", {"query": "workflow"})

    assert "shared-workflow" in result[0].text
    assert "legacy-workflow" not in result[0].text


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_grants_once_and_returns_skill_contents(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "obsidian-knowledge")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    monkeypatch.delenv("PYNCHY_PROFILE_SKILLS_ROOT", raising=False)
    request = AsyncMock(
        side_effect=[
            _response({"status": "available"}),
            _response({"answers": {"answer": "Grant once"}}),
        ]
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access",
        {"skill_name": "obsidian-knowledge", "reason": "I need prior project context."},
    )

    assert "Access granted" in result[0].text
    assert '<skill name="obsidian-knowledge">' in result[0].text
    options = request.await_args_list[1].args[1]["questions"][0]["options"]
    assert request.await_args_list[1].args[1]["questions"][0]["skill_access"] == {
        "skill_name": "obsidian-knowledge"
    }
    assert [option["label"] for option in options] == [
        "Grant once",
        "Grant always",
        "Deny once",
        "Deny always",
    ]
    assert request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_persists_an_always_grant(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "pynchy-operations")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    monkeypatch.delenv("PYNCHY_PROFILE_SKILLS_ROOT", raising=False)
    request = AsyncMock(
        side_effect=[
            _response({"status": "available"}),
            _response(
                {
                    "answers": {"answer": "Grant always"},
                    "skill_access_status": "granted",
                }
            ),
        ]
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access",
        {"skill_name": "pynchy-operations", "reason": "I need live Pynchy guidance."},
    )

    assert "future turns through this profile" in result[0].text
    assert request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_persists_an_always_denial(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "pynchy-operations")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    monkeypatch.delenv("PYNCHY_PROFILE_SKILLS_ROOT", raising=False)
    request = AsyncMock(
        side_effect=[
            _response({"status": "available"}),
            _response(
                {
                    "answers": {"answer": "Deny always"},
                    "skill_access_status": "denied",
                }
            ),
        ]
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access",
        {"skill_name": "pynchy-operations", "reason": "I need live Pynchy guidance."},
    )

    assert result[0].text == "Access to 'pynchy-operations' was denied permanently."
    assert request.await_count == 2
