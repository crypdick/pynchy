"""Built-in X (Twitter) integration plugin (service handler)."""

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
