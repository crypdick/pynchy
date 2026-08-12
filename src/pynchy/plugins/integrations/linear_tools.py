"""MCP tool schemas for the built-in Linear integration."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

_LINEAR_LABEL_IDS_NOT_ARRAY = "label_ids must be an array of Linear label ids"
_LINEAR_PRIORITY_INVALID = "priority must be an integer from 0 through 4"


class UnknownLinearToolError(ValueError):
    """Requested Linear MCP tool is not registered."""


class LinearToolArgumentsError(ValueError):
    """Linear MCP arguments are not a JSON object."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListTeamsArguments(_StrictModel):
    """Arguments for team listing."""


class ListIssuesArguments(_StrictModel):
    team_id: str | None = None
    first: int = Field(default=50, ge=1, le=100)


class SearchIssuesArguments(ListIssuesArguments):
    query: str = Field(min_length=1)


class GetIssueArguments(_StrictModel):
    issue_id: str = Field(min_length=1)


class CreateIssueArguments(_StrictModel):
    team_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str | None = None
    project_id: str | None = None
    label_ids: list[str] | None = None
    priority: int | None = None

    @field_validator("label_ids", mode="before")
    @classmethod
    def _validate_label_ids(cls, value: object) -> object:
        if value is not None and not isinstance(value, list):
            raise ValueError(_LINEAR_LABEL_IDS_NOT_ARRAY)
        return value

    @field_validator("priority", mode="before")
    @classmethod
    def _validate_priority(cls, value: object) -> object:
        return _priority(value)


class ListTodosArguments(_StrictModel):
    include_done: bool = False


class CreateTodoArguments(_StrictModel):
    title: str = Field(min_length=1)
    description: str | None = None
    priority: int | None = None

    @field_validator("priority", mode="before")
    @classmethod
    def _validate_priority(cls, value: object) -> object:
        return _priority(value)


class CreateAttachmentArguments(_StrictModel):
    issue_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    subtitle: str | None = None


class FindIssuesByAttachmentUrlArguments(_StrictModel):
    url: str = Field(min_length=1)


class ListTeamsCall(_StrictModel):
    name: Literal["linear_list_teams"]
    arguments: ListTeamsArguments = Field(default_factory=ListTeamsArguments)


class ListIssuesCall(_StrictModel):
    name: Literal["linear_list_issues"]
    arguments: ListIssuesArguments = Field(default_factory=ListIssuesArguments)


class SearchIssuesCall(_StrictModel):
    name: Literal["linear_search_issues"]
    arguments: SearchIssuesArguments


class GetIssueCall(_StrictModel):
    name: Literal["linear_get_issue"]
    arguments: GetIssueArguments


class CreateIssueCall(_StrictModel):
    name: Literal["linear_create_issue"]
    arguments: CreateIssueArguments


class ListTodosCall(_StrictModel):
    name: Literal["linear_list_todos"]
    arguments: ListTodosArguments = Field(default_factory=ListTodosArguments)


class CreateTodoCall(_StrictModel):
    name: Literal["linear_create_todo"]
    arguments: CreateTodoArguments


class CreateAttachmentCall(_StrictModel):
    name: Literal["linear_create_attachment"]
    arguments: CreateAttachmentArguments


class FindIssuesByAttachmentUrlCall(_StrictModel):
    name: Literal["linear_find_issues_by_attachment_url"]
    arguments: FindIssuesByAttachmentUrlArguments


type LinearToolCall = Annotated[
    ListTeamsCall
    | ListIssuesCall
    | SearchIssuesCall
    | GetIssueCall
    | CreateIssueCall
    | ListTodosCall
    | CreateTodoCall
    | CreateAttachmentCall
    | FindIssuesByAttachmentUrlCall,
    Field(discriminator="name"),
]
_TOOL_CALL_ADAPTER: TypeAdapter[LinearToolCall] = TypeAdapter(LinearToolCall)
_TOOL_NAMES = frozenset(
    {
        "linear_list_teams",
        "linear_list_issues",
        "linear_search_issues",
        "linear_get_issue",
        "linear_create_issue",
        "linear_list_todos",
        "linear_create_todo",
        "linear_create_attachment",
        "linear_find_issues_by_attachment_url",
    }
)


class _ToolCallEnvelope(_StrictModel):
    name: str
    arguments: object = Field(default_factory=dict)


def parse_tool_call(params: object) -> LinearToolCall:
    """Parse untrusted MCP parameters into one semantic tool call."""
    try:
        envelope = _ToolCallEnvelope.model_validate(params)
    except ValidationError as exc:
        raise LinearToolArgumentsError(_validation_error_message(exc)) from None
    if envelope.name not in _TOOL_NAMES:
        raise UnknownLinearToolError(envelope.name)
    if not isinstance(envelope.arguments, dict):
        raise LinearToolArgumentsError("Tool arguments must be an object")
    try:
        return _TOOL_CALL_ADAPTER.validate_python(
            {"name": envelope.name, "arguments": envelope.arguments}
        )
    except ValidationError as exc:
        raise LinearToolArgumentsError(_validation_error_message(exc)) from None


def tool_specs() -> list[dict[str, object]]:
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


def _priority(value: object) -> object:
    if value is not None and (type(value) is not int or not 0 <= value <= 4):
        raise ValueError(_LINEAR_PRIORITY_INVALID)
    return value


def _validation_error_message(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = error["loc"]
    field = str(location[-1]) if location else "arguments"
    error_type = str(error["type"])
    if error_type == "missing":
        return f"{field} is required"
    if error_type == "extra_forbidden":
        return f"unexpected arguments: {field}"
    if field == "first":
        return "first must be an integer from 1 through 100"
    if error_type == "string_type":
        return f"{field} must be a string"
    return str(exc)
