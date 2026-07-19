"""MCP tool schemas for the built-in Linear integration."""

from __future__ import annotations

from typing import Any


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "linear_list_teams",
            "description": "List Linear teams available to the configured API key.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "linear_list_issues",
            "description": "List recent Linear issues, optionally scoped to a team id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "team_id": {"type": "string"},
                    "first": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "linear_create_issue",
            "description": "Create a Linear issue in a team.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "team_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "project_id": {"type": "string"},
                    "label_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["team_id", "title"],
                "additionalProperties": False,
            },
        },
        {
            "name": "linear_list_todos",
            "description": "List Linear todo issues for this Pynchy workspace.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "include_done": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "linear_create_todo",
            "description": (
                "Propose a Linear work item for this Pynchy workspace. "
                "The item starts in Agent Proposed; this tool cannot assert human approval."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    ]
