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
async def test_search_skills_matches_live_operations_morphology(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "agent-harness-operations",
        "Use when inspecting, operating, or improving the Pynchy agent harness.",
    )
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))

    result = await call_tool("search_skills", {"query": "Pynchy operations inspection"})

    assert "agent-harness-operations" in result[0].text


@pytest.mark.asyncio
@pytest.mark.action("skill.catalog.search")
async def test_search_skills_matches_verbose_live_operations_query(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "agent-harness-operations",
        "Inspect and operate the Pynchy agent harness, including service health and logs.",
    )
    _write_skill(
        root,
        "pynchy-operations",
        "Safely inspect Pynchy service status and runtime reconciliation.",
    )
    _write_skill(
        root,
        "service-diagnostics",
        "Monitor service status, logs, and diagnostics for an unrelated database.",
    )
    _write_skill(root, "pynchy-release-notes", "Read Pynchy release notes.")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))

    result = await call_tool(
        "search_skills",
        {"query": "inspect Pynchy operations operational status service logs diagnostics"},
    )

    assert "agent-harness-operations" in result[0].text
    assert "pynchy-operations" in result[0].text
    assert "service-diagnostics" not in result[0].text
    assert "pynchy-release-notes" not in result[0].text


@pytest.mark.asyncio
@pytest.mark.action("skill.catalog.search")
async def test_search_skills_prefers_strongest_normalized_coverage(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "agent-harness-operations",
        "Use when inspecting, operating, or improving the Pynchy agent harness.",
    )
    _write_skill(root, "pynchy-release-notes", "Read Pynchy release notes.")
    _write_skill(
        root,
        "industrial-inspection",
        "Use when inspecting and operating industrial machinery.",
    )
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))

    result = await call_tool("search_skills", {"query": "Pynchy operations inspection"})

    assert "agent-harness-operations" in result[0].text
    assert "pynchy-release-notes" not in result[0].text
    assert "industrial-inspection" not in result[0].text


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
@pytest.mark.action("skill.catalog.search")
async def test_search_skills_reports_invalid_queries_and_missing_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(tmp_path / "missing"))

    empty_result = await call_tool("search_skills", {"query": "  "})
    unavailable_result = await call_tool("search_skills", {"query": "operations"})

    assert empty_result[0].text == "Provide a non-empty skill search query."
    assert unavailable_result[0].text == "The Pynchy skill catalog is unavailable in this session."


@pytest.mark.asyncio
@pytest.mark.action("skill.catalog.search")
async def test_search_skills_reports_no_match_and_uses_heading_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "heading-only"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Heading fallback\n", encoding="utf-8")
    (root / "not-a-skill").write_text("ignored", encoding="utf-8")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))

    match = await call_tool("search_skills", {"query": "heading"})
    no_match = await call_tool("search_skills", {"query": "unrelated"})

    assert "heading-only: Heading fallback" in match[0].text
    assert no_match[0].text == "No Pynchy skills matched 'unrelated'."


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


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_rejects_missing_and_unknown_skills(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "known-skill")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))

    missing = await call_tool("request_skill_access", {"skill_name": "known-skill"})
    unknown = await call_tool(
        "request_skill_access", {"skill_name": "missing", "reason": "Need it."}
    )

    assert missing.content[0].text == "skill_name and reason are required"
    assert unknown.content[0].text == "Unknown Pynchy skill: 'missing'. Search the catalog first."


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_returns_existing_grant_without_asking(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "known-skill")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    request = AsyncMock(return_value=_response({"status": "granted"}))
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access", {"skill_name": "known-skill", "reason": "Need it."}
    )

    assert "future turns through this profile" in result[0].text
    assert request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_returns_existing_denial_without_asking(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "known-skill")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    request = AsyncMock(return_value=_response({"status": "denied"}))
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access", {"skill_name": "known-skill", "reason": "Need it."}
    )

    assert result.content[0].text == "Access to 'known-skill' is denied by this workspace profile."
    assert request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_reports_policy_check_failure(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "known-skill")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    request = AsyncMock(return_value=_response({"status": "error"}))
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access", {"skill_name": "known-skill", "reason": "Need it."}
    )

    assert result.content[0].text == "Unable to check access for 'known-skill'."


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_handles_malformed_question_response(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "known-skill")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    request = AsyncMock(
        side_effect=[
            [TextContent(type="text", text="not json")],
            [TextContent(type="text", text="[]")],
        ]
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access", {"skill_name": "known-skill", "reason": "Need it."}
    )

    assert result.content[0].text == "The skill-access question did not return a valid choice."


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_handles_one_time_denial_and_unknown_choice(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "known-skill")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    request = AsyncMock(
        side_effect=[
            _response({"status": "available"}),
            _response({"answers": {"answer": "Deny once"}}),
            _response({"status": "available"}),
            _response({"answers": {"answer": "Maybe"}}),
        ]
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    denied = await call_tool(
        "request_skill_access", {"skill_name": "known-skill", "reason": "Need it."}
    )
    unknown = await call_tool(
        "request_skill_access", {"skill_name": "known-skill", "reason": "Need it."}
    )

    assert denied[0].text == "Access to 'known-skill' was denied for now."
    assert unknown.content[0].text == "Unrecognized skill-access choice: maybe"


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_reports_failed_persistent_policy(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "known-skill")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    request = AsyncMock(
        side_effect=[
            _response({"status": "available"}),
            _response({"answers": {"answer": "Grant always"}, "skill_access_status": "error"}),
        ]
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access", {"skill_name": "known-skill", "reason": "Need it."}
    )

    assert result.content[0].text == "Unable to persist access policy for 'known-skill'."


@pytest.mark.asyncio
@pytest.mark.action("skill.access.request")
async def test_request_skill_access_truncates_large_skill_content(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "large-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("x" * 40_001, encoding="utf-8")
    monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(root))
    request = AsyncMock(
        side_effect=[
            _response({"status": "available"}),
            _response({"answers": {"answer": "Grant once"}}),
        ]
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_skills.ipc_service_request", request)

    result = await call_tool(
        "request_skill_access", {"skill_name": "large-skill", "reason": "Need it."}
    )

    assert result[0].text.endswith("[Skill truncated at 40,000 characters.]\n</skill>")
