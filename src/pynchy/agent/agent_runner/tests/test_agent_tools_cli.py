"""End-to-end checks for Pynchy's native stdio MCP server."""

from __future__ import annotations

import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_native_mcp_advertises_and_calls_skill_discovery(tmp_path) -> None:
    skills = tmp_path / "skills"
    skill = skills / "job-hunt-tracking"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: job-hunt-tracking\n"
        "description: Track unemployment benefits and myEDD evidence.\n---\n"
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_runner.agent_tools"],
        env={**os.environ, "PYNCHY_SKILLS_ROOT": str(skills)},
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialization = await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("search_skills", {"query": "myEDD unemployment"})

    assert initialization.instructions is not None
    skill_tool_names = {"search_skills", "request_skill_access"}
    assert all(name in initialization.instructions for name in skill_tool_names)
    assert skill_tool_names <= {tool.name for tool in tools.tools}
    assert "job-hunt-tracking" in result.content[0].text
