"""Built-in Tailscale tunnel plugin.

Detects Tailscale connectivity on the host by calling ``tailscale status --json``.
Registered automatically during plugin discovery (``builtin_*.py`` convention).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class _TailscaleTunnel:
    """Tailscale tunnel provider.

    Caches the result of ``tailscale status --json`` so that
    ``is_connected()`` and ``status_summary()`` don't shell out twice.
    """

    name: str = "tailscale"

    def __init__(self) -> None:
        self._backend_state: str | None = None
        self._error: str | None = None
        self._fetched = False

    def _fetch(self) -> None:
        if self._fetched:
            return
        self._fetched = True
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                self._error = f"exit code {result.returncode}"
                return
            status = json.loads(result.stdout)
            self._backend_state = status.get("BackendState", "unknown")
        except FileNotFoundError:
            self._error = "CLI not found"
        except Exception as exc:
            self._error = str(exc)

    def is_available(self) -> bool:
        return shutil.which("tailscale") is not None

    def is_connected(self) -> bool:
        self._fetch()
        return self._backend_state == "Running"

    def status_summary(self) -> str:
        self._fetch()
        if self._error:
            return self._error
        return f"BackendState={self._backend_state}"


class TailscaleTunnelPlugin:
    """Built-in plugin providing Tailscale tunnel detection."""

    @hookimpl
    def pynchy_tunnel(self) -> _TailscaleTunnel:
        return _TailscaleTunnel()
