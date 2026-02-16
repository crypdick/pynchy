"""Email tools — read and send email via IPC service requests.

These tools write IPC requests that the host processes after applying
policy middleware. Actual email service integration comes in Step 3.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from agent_runner.agent_tools._ipc_request import ipc_service_request
from agent_runner.agent_tools._registry import ToolEntry, register

# --- read_email ---


def _read_email_definition() -> Tool:
    return Tool(
        name="read_email",
        description="Read emails matching filter criteria.",
        inputSchema={
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Email folder to read from (default: INBOX)",
                    "default": "INBOX",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of emails to return (default: 10)",
                    "default": 10,
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Only return unread emails (default: false)",
                    "default": False,
                },
            },
        },
    )


async def _read_email_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "read_email",
        {
            "folder": arguments.get("folder", "INBOX"),
            "limit": arguments.get("limit", 10),
            "unread_only": arguments.get("unread_only", False),
        },
    )


# --- send_email ---


def _send_email_definition() -> Tool:
    return Tool(
        name="send_email",
        description=(
            "Send an email. This is a high-risk action that may require "
            "human approval before the email is actually sent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line",
                },
                "body": {
                    "type": "string",
                    "description": "Email body text",
                },
                "cc": {
                    "type": "string",
                    "description": "CC recipients (comma-separated)",
                },
                "bcc": {
                    "type": "string",
                    "description": "BCC recipients (comma-separated)",
                },
            },
            "required": ["to", "subject", "body"],
        },
    )


async def _send_email_handle(arguments: dict) -> list[TextContent]:
    request = {
        "to": arguments["to"],
        "subject": arguments["subject"],
        "body": arguments["body"],
    }
    if arguments.get("cc"):
        request["cc"] = arguments["cc"]
    if arguments.get("bcc"):
        request["bcc"] = arguments["bcc"]
    return await ipc_service_request("send_email", request)


register("read_email", ToolEntry(definition=_read_email_definition, handler=_read_email_handle))
register("send_email", ToolEntry(definition=_send_email_definition, handler=_send_email_handle))
