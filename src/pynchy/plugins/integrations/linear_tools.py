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
            "name": "linear_get_issue",
            "description": "Get one Linear issue by its stable id.",
            "inputSchema": {
                "type": "object",
                "properties": {"issue_id": {"type": "string"}},
                "required": ["issue_id"],
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
                "The item starts in Agent Proposed and may include evidence and acceptance "
                "criteria; this tool cannot assert human approval."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {
                        "type": "integer",
                        "enum": [0, 1, 2, 3, 4],
                        "description": (
                            "Linear priority: 0 none, 1 urgent, 2 high, 3 medium, 4 low."
                        ),
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    ]
