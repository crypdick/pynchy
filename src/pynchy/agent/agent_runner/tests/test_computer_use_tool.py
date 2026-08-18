"""Tests for the computer_use MCP tool registration."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import enabled_agent_tools, list_tools


@pytest.mark.asyncio
async def test_computer_use_tool_is_advertised() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    assert "computer_use" in tools
    schema = tools["computer_use"].inputSchema
    assert schema["properties"]["action"]["enum"] == [
        "capture",
        "list_apps",
        "list_windows",
        "launch_app",
        "click",
        "double_click",
        "right_click",
        "type",
        "key",
        "scroll",
        "set_value",
        "perform_action",
        "menu_list",
        "menu_click",
        "dialog_list",
        "dialog_click",
        "dialog_input",
        "dialog_file",
        "dialog_dismiss",
        "clipboard_get",
        "clipboard_set",
        "clipboard_clear",
        "clipboard_save",
        "clipboard_restore",
        "space_list",
        "space_switch",
        "space_move_window",
        "wait",
        "check_permissions",
    ]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["element"]["oneOf"] == [
        {"type": "string", "minLength": 1},
        {"type": "integer", "minimum": 1},
    ]
    assert schema["properties"]["keys"]["oneOf"][1]["minItems"] == 1


def test_computer_use_grant_limits_workspace_tool_families() -> None:
    tools = set(enabled_agent_tools(["computer_use"]))

    assert {"computer_use", "take_screenshot", "search_skills"} <= tools
    assert {"list_calendars", "gog_gmail_search", "linear_submit_plan"}.isdisjoint(tools)
