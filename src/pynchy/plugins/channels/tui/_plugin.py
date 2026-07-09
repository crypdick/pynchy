"""TUI channel plugin implementation."""

from __future__ import annotations

from typing import Any

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class TuiChannelPlugin:
    """Plugin packaging the TUI client alongside other channel plugins."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> Any | None:
        # TUI uses the HTTP/SSE server directly — no Channel instance needed.
        del context
        return None
