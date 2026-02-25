"""ask_user tool — routes questions to the user via the host messaging channel.

Replaces the built-in ``AskUserQuestion`` tool.  The agent calls this to
ask the user one or more questions; the host forwards them to the messaging
channel (Slack/WhatsApp) and blocks until a reply arrives.

Uses its own IPC type prefix (``ask_user:``) instead of the generic
``service:`` prefix so the host can dispatch it to a dedicated handler.
"""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent, Tool

from agent_runner.agent_tools._ipc_request import ipc_service_request
from agent_runner.agent_tools._registry import ToolEntry, register, tool_error

ASK_USER_TIMEOUT = 1800  # 30 minutes — user may take a while to reply


# ---------------------------------------------------------------------------
# Tool definition and handler
# ---------------------------------------------------------------------------


def _ask_user_definition() -> Tool:
    return Tool(
        name="ask_user",
        description=(
            "Ask the user one or more questions and wait for their reply. "
            "The question is forwarded to the messaging channel "
            "(Slack/WhatsApp) and the tool blocks until the user responds. "
            "Use this when you need user input, confirmation, or a decision "
            "before proceeding.\n\n"
            "Each question can optionally include predefined options for "
            "the user to choose from."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The question to ask the user",
                            },
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label", "description"],
                                },
                                "description": "Optional predefined answer options",
                            },
                        },
                        "required": ["question"],
                    },
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "List of questions to ask the user (1-4)",
                },
            },
            "required": ["questions"],
        },
    )


async def _ask_user_handle(arguments: dict) -> list[TextContent] | CallToolResult:
    questions = arguments.get("questions")
    if not questions:
        return tool_error("questions list must be non-empty")
    return await ipc_service_request(
        "ask_user",
        {"questions": questions},
        timeout=ASK_USER_TIMEOUT,
        type_override="ask_user:ask",
    )


register(
    "ask_user",
    ToolEntry(definition=_ask_user_definition, handler=_ask_user_handle),
)
