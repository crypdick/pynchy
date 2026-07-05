"""Built-in X (Twitter) integration plugin (service handler).

Provides host-side handlers for X/Twitter actions (post, like, reply, retweet,
quote) via Playwright browser automation with a persistent Chromium profile.
Uses the system Chrome binary (``CHROME_PATH``) in headed mode to avoid
X's bot detection — Playwright's bundled Chromium is never used.

Six handlers:
- ``setup_x_session`` — headed browser for manual X login (noVNC on headless servers)
- ``x_post`` — post a tweet (max 280 chars)
- ``x_like`` — like a tweet
- ``x_reply`` — reply to a tweet
- ``x_retweet`` — retweet
- ``x_quote`` — quote tweet with comment

The container-side IPC relay (_tools_x.py) sends service requests through IPC;
the host service handler dispatches to these handlers after policy enforcement.

Implementation is split across this package:

- ``_display``: persistent Xvfb + noVNC lifecycle (headed mode avoids bot detection)
- ``_browser``: Playwright selectors, timeouts, and launch/session helpers
- ``_actions``: the six service-tool handler functions
"""

from __future__ import annotations

from typing import Any

import pluggy

from pynchy.plugins.integrations.browser import check_browser_plugin_deps
from pynchy.plugins.integrations.x_integration._actions import (
    handle_setup_x_session,
    handle_x_like,
    handle_x_post,
    handle_x_quote,
    handle_x_reply,
    handle_x_retweet,
)

hookimpl = pluggy.HookimplMarker("pynchy")

# Validate browser deps at import time so failures surface on plugin load
check_browser_plugin_deps("setup_x_session")


class XIntegrationPlugin:
    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Any]:
        return {
            "tools": {
                "setup_x_session": handle_setup_x_session,
                "x_post": handle_x_post,
                "x_like": handle_x_like,
                "x_reply": handle_x_reply,
                "x_retweet": handle_x_retweet,
                "x_quote": handle_x_quote,
            },
        }
