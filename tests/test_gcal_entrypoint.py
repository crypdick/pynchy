"""Regression coverage for the Calendar MCP Docker listener."""

from __future__ import annotations

from pathlib import Path


def test_gcal_entrypoint_listens_on_all_interfaces() -> None:
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "pynchy"
        / "agent"
        / "mcp"
        / "gcal-entrypoint.sh"
    )

    contents = entrypoint.read_text()

    assert "google-calendar-mcp --transport http --host 0.0.0.0" in contents
    assert "Google Calendar MCP requires Google setup" in contents
