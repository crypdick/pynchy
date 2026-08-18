"""Tests for Codex's local bridge into Pynchy agent tools."""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - tests run the fixed agent-tools module argv.
import sys


def test_call_hex_invokes_visible_agent_tool(tmp_path) -> None:
    skills = tmp_path / "skills"
    skill = skills / "job-hunt-tracking"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: job-hunt-tracking\n"
        "description: Track unemployment benefits and myEDD evidence.\n---\n"
    )
    arguments = json.dumps({"query": "myEDD unemployment"}).encode().hex()
    env = {**os.environ, "PYNCHY_SKILLS_ROOT": str(skills)}

    result = subprocess.run(  # noqa: S603 - fixed interpreter and module argv.
        [
            sys.executable,
            "-m",
            "agent_runner.agent_tools",
            "call-hex",
            "search_skills",
            arguments,
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0
    assert "job-hunt-tracking" in json.loads(result.stdout)["content"][0]["text"]


def test_call_hex_rejects_invalid_arguments() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_runner.agent_tools", "call-hex", "search_skills", "xyz"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "arguments must be hex-encoded JSON object" in result.stderr


def test_call_hex_cannot_invoke_hidden_agent_tool() -> None:
    arguments = (
        json.dumps({"jid": "chat", "name": "chat", "folder": "chat", "trigger": "@pynchy"})
        .encode()
        .hex()
    )
    env = {**os.environ, "PYNCHY_IS_ADMIN": "0"}

    result = subprocess.run(  # noqa: S603 - fixed interpreter and module argv.
        [
            sys.executable,
            "-m",
            "agent_runner.agent_tools",
            "call-hex",
            "register_group",
            arguments,
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["isError"] is True
    assert json.loads(result.stdout)["content"][0]["text"] == "Unknown tool: register_group"
