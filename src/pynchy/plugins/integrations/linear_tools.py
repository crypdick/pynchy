"""MCP tool schemas for the built-in Linear integration."""

from __future__ import annotations

from typing import Any


def tool_specs() -> list[dict[str, Any]]:
    # NOTE: Keep docs/integrations/linear.md § Use Linear tools aligned with this list.
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
            "name": "linear_search_issues",
            "description": "Find Linear issues by case-insensitive title text.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "team_id": {"type": "string"},
                    "first": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["query"],
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
                    "priority": {
                        "type": "integer",
                        "enum": [0, 1, 2, 3, 4],
                        "description": (
                            "Linear priority: 0 none, 1 urgent, 2 high, 3 medium, 4 low."
                        ),
                    },
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
            "description": "Create an Agent Proposed work item for this workspace.",
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
        {
            "name": "linear_create_attachment",
            "description": (
                "Attach an external URL to a Linear issue. Use one attachment for every "
                "pull request produced by the work. Reusing the same issue and URL updates "
                "the existing attachment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "url": {"type": "string", "format": "uri"},
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                },
                "required": ["issue_id", "url", "title"],
                "additionalProperties": False,
            },
        },
        {
            "name": "linear_find_issues_by_attachment_url",
            "description": (
                "Find Linear issues linked to an exact external URL. Use the canonical pull "
                "request URL from a GitHub event to recover its attached work item."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    ]
