"""Public agent-runner prompt and direct-MCP boundary behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"),
)

from agent_runner.main import (
    apply_followup_metadata,
    build_agent_prompt,
    build_core_config,
    build_initial_prompt,
)
from agent_runner.models import ContainerInput


def _input(**kwargs: object) -> ContainerInput:
    values: dict[str, object] = {
        "messages": [],
        "group_folder": "review",
        "chat_jid": "chat:review",
        "is_admin": False,
    }
    values.update(kwargs)
    return ContainerInput(**values)


def test_system_notices_precede_the_agent_prompt() -> None:
    input_data = _input(
        messages=[{"sender_name": "Operator", "content": "Continue."}],
        chat_jid="scheduled:review",
        system_notices=["Worktree has unpushed commits", "A tool is unavailable"],
    )

    prompt = build_agent_prompt(input_data)

    assert prompt.startswith(
        "[System Notice] Worktree has unpushed commits\n[System Notice] A tool is unavailable\n\n"
    )
    assert prompt.endswith(">Continue.</message>\n</messages>")


def test_initial_prompt_appends_pending_ipc_messages() -> None:
    with patch("agent_runner.main.drain_ipc_input", return_value=["A new message"]):
        prompt = build_initial_prompt(_input())

    assert prompt == "\nA new message"


def test_followup_metadata_ignores_an_empty_update() -> None:
    config = build_core_config(_input(agent_core_config={"metadata": {"existing": "value"}}))

    apply_followup_metadata(config, turn_id=None, metadata={})

    assert config.extra == {"metadata": {"existing": "value"}}


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ({"name": "remote", "url": "http://remote", "transport": 1}, "transport"),
        ({"name": "remote", "url": None}, "URL"),
    ],
)
def test_direct_mcp_servers_require_string_transport_and_url(
    server: dict[str, object], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        build_core_config(_input(mcp_direct_servers=[server]))


def test_direct_mcp_server_preserves_unknown_transport() -> None:
    config = build_core_config(
        _input(
            mcp_direct_servers=[{"name": "remote", "url": "http://remote", "transport": "custom"}]
        )
    )

    assert config.mcp_servers["remote"] == {"type": "custom", "url": "http://remote"}
