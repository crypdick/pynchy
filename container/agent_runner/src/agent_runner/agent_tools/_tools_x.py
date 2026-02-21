"""X (Twitter) tools — post, like, reply, retweet, and quote via IPC service requests.

Automates X/Twitter actions via Playwright browser automation with a persistent
Chromium profile on the host.  Uses the system Chrome binary in headed mode
to avoid X's bot detection.

These tools write IPC requests that the host processes after applying
policy middleware.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from agent_runner.agent_tools._ipc_request import ipc_service_request
from agent_runner.agent_tools._registry import ToolEntry, register

# --- setup_x_session ---


def _setup_x_session_definition() -> Tool:
    return Tool(
        name="setup_x_session",
        description=(
            "Launch a headed browser for manual X login. Saves the session "
            "to a persistent profile for future automated use. On headless "
            "servers, automatically starts a virtual display with noVNC."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "integer",
                    "description": "How long to wait for login completion (default: 120s)",
                    "default": 120,
                },
            },
        },
    )


async def _setup_x_session_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "setup_x_session",
        {"timeout_seconds": arguments.get("timeout_seconds", 120)},
    )


# --- x_post ---


def _x_post_definition() -> Tool:
    return Tool(
        name="x_post",
        description="Post a tweet on X (Twitter).",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The tweet text (max 280 characters)",
                },
            },
            "required": ["content"],
        },
    )


async def _x_post_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request("x_post", {"content": arguments["content"]})


# --- x_like ---


def _x_like_definition() -> Tool:
    return Tool(
        name="x_like",
        description="Like a tweet on X (Twitter).",
        inputSchema={
            "type": "object",
            "properties": {
                "tweet_url": {
                    "type": "string",
                    "description": (
                        "URL of the tweet (e.g. https://x.com/user/status/123) or a bare tweet ID"
                    ),
                },
            },
            "required": ["tweet_url"],
        },
    )


async def _x_like_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request("x_like", {"tweet_url": arguments["tweet_url"]})


# --- x_reply ---


def _x_reply_definition() -> Tool:
    return Tool(
        name="x_reply",
        description="Reply to a tweet on X (Twitter).",
        inputSchema={
            "type": "object",
            "properties": {
                "tweet_url": {
                    "type": "string",
                    "description": (
                        "URL of the tweet to reply to (e.g. https://x.com/user/status/123) "
                        "or a bare tweet ID"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The reply text (max 280 characters)",
                },
            },
            "required": ["tweet_url", "content"],
        },
    )


async def _x_reply_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "x_reply",
        {"tweet_url": arguments["tweet_url"], "content": arguments["content"]},
    )


# --- x_retweet ---


def _x_retweet_definition() -> Tool:
    return Tool(
        name="x_retweet",
        description="Retweet a tweet on X (Twitter).",
        inputSchema={
            "type": "object",
            "properties": {
                "tweet_url": {
                    "type": "string",
                    "description": (
                        "URL of the tweet to retweet (e.g. https://x.com/user/status/123) "
                        "or a bare tweet ID"
                    ),
                },
            },
            "required": ["tweet_url"],
        },
    )


async def _x_retweet_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request("x_retweet", {"tweet_url": arguments["tweet_url"]})


# --- x_quote ---


def _x_quote_definition() -> Tool:
    return Tool(
        name="x_quote",
        description="Quote tweet with a comment on X (Twitter).",
        inputSchema={
            "type": "object",
            "properties": {
                "tweet_url": {
                    "type": "string",
                    "description": (
                        "URL of the tweet to quote (e.g. https://x.com/user/status/123) "
                        "or a bare tweet ID"
                    ),
                },
                "comment": {
                    "type": "string",
                    "description": "The comment text (max 280 characters)",
                },
            },
            "required": ["tweet_url", "comment"],
        },
    )


async def _x_quote_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "x_quote",
        {"tweet_url": arguments["tweet_url"], "comment": arguments["comment"]},
    )


register(
    "setup_x_session",
    ToolEntry(definition=_setup_x_session_definition, handler=_setup_x_session_handle),
)
register("x_post", ToolEntry(definition=_x_post_definition, handler=_x_post_handle))
register("x_like", ToolEntry(definition=_x_like_definition, handler=_x_like_handle))
register("x_reply", ToolEntry(definition=_x_reply_definition, handler=_x_reply_handle))
register("x_retweet", ToolEntry(definition=_x_retweet_definition, handler=_x_retweet_handle))
register("x_quote", ToolEntry(definition=_x_quote_definition, handler=_x_quote_handle))
