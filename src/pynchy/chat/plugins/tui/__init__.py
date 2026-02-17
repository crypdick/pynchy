"""TUI channel plugin.

The TUI is a standalone Textual client that connects to the pynchy HTTP server.
Unlike other channel plugins (Slack, WhatsApp), the TUI does not implement
the Channel protocol — it communicates via the HTTP/SSE endpoints instead.

This plugin packages the TUI client code alongside other channels for
consistent organization.  It returns ``None`` from the channel hook since
the server-side TUI support is handled by the HTTP server directly.
"""

from __future__ import annotations

from typing import Any

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class TuiChannelPlugin:
    """Plugin packaging the TUI client alongside other channel plugins."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> Any | None:
        # TUI uses the HTTP/SSE server directly — no Channel instance needed.
        return None
