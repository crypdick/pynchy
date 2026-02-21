"""X (Twitter) tools — post, like, reply, retweet, and quote via IPC service requests.

Automates X/Twitter actions via Playwright browser automation with a persistent
Chromium profile on the host.  Uses the system Chrome binary in headed mode
to avoid X's bot detection.

These tools write IPC requests that the host processes after applying
policy middleware.
"""

from agent_runner.agent_tools._registry import register_ipc_tool

register_ipc_tool(
    name="setup_x_session",
    description=(
        "Launch a headed browser for manual X login. Saves the session "
        "to a persistent profile for future automated use. On headless "
        "servers, automatically starts a virtual display with noVNC."
    ),
    input_schema={
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

register_ipc_tool(
    name="x_post",
    description="Post a tweet on X (Twitter).",
    input_schema={
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

register_ipc_tool(
    name="x_like",
    description="Like a tweet on X (Twitter).",
    input_schema={
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

register_ipc_tool(
    name="x_reply",
    description="Reply to a tweet on X (Twitter).",
    input_schema={
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

register_ipc_tool(
    name="x_retweet",
    description="Retweet a tweet on X (Twitter).",
    input_schema={
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

register_ipc_tool(
    name="x_quote",
    description="Quote tweet with a comment on X (Twitter).",
    input_schema={
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
