"""Tests for the typed Gog IPC tool registrations."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


@pytest.mark.asyncio
async def test_gog_tools_are_advertised_without_a_generic_command_escape_hatch() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    assert set(tools) >= {
        "gog_setup_start",
        "gog_setup_complete",
        "gog_gmail_search",
        "gog_gmail_get",
        "gog_gmail_create_draft",
        "gog_gmail_send_draft",
        "gog_gmail_send",
        "gog_contacts_search",
        "gog_docs_read",
        "gog_docs_export",
        "gog_sheets_get",
        "gog_sheets_update",
    }
    assert "gog" not in tools
    assert tools["gog_gmail_send"].inputSchema["required"] == ["to", "subject", "body"]
    assert tools["gog_sheets_update"].inputSchema["required"] == [
        "spreadsheet_id",
        "range",
        "values",
    ]
