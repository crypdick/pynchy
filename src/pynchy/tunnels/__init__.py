"""Tunnel provider detection with plugin-extensible providers.

Tailscale is built in. Additional tunnel providers (Cloudflare Tunnel,
WireGuard, etc.) can be provided by plugins via ``pynchy_tunnel``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pynchy.logger import logger

if TYPE_CHECKING:
    import pluggy

__all__ = [
    "TunnelProvider",
    "check_tunnels",
]


@runtime_checkable
class TunnelProvider(Protocol):
    """Tunnel provider contract implemented by built-ins and plugins."""

    name: str

    def is_available(self) -> bool: ...
    def is_connected(self) -> bool: ...
    def status_summary(self) -> str: ...


def _is_valid_tunnel_provider(candidate: Any) -> bool:
    return all(
        [
            hasattr(candidate, "name"),
            callable(getattr(candidate, "is_available", None)),
            callable(getattr(candidate, "is_connected", None)),
            callable(getattr(candidate, "status_summary", None)),
        ]
    )


def check_tunnels(pm: pluggy.PluginManager) -> None:
    """Check all registered tunnel providers, warn if none connected.

    Non-fatal: logs warnings but never raises.
    """
    try:
        provided = pm.hook.pynchy_tunnel()
    except Exception:
        logger.exception("Failed to resolve tunnel plugins")
        return

    tunnels: list[TunnelProvider] = []
    for tunnel in provided:
        if tunnel is None:
            continue
        if not _is_valid_tunnel_provider(tunnel):
            logger.warning(
                "Ignoring invalid tunnel plugin object",
                tunnel_type=type(tunnel).__name__,
            )
            continue
        tunnels.append(tunnel)

    if not tunnels:
        logger.info("No tunnel plugins registered")
        return

    connected: list[str] = []
    for t in tunnels:
        try:
            if not t.is_available():
                logger.info("Tunnel not available on this host", tunnel=t.name)
                continue
            if t.is_connected():
                logger.info("Tunnel connected", tunnel=t.name, status=t.status_summary())
                connected.append(t.name)
            else:
                logger.warning(
                    "Tunnel not connected",
                    tunnel=t.name,
                    status=t.status_summary(),
                )
        except Exception as exc:
            logger.warning("Tunnel check failed", tunnel=t.name, err=str(exc))

    if not connected:
        logger.warning(
            "No tunnels connected — remote access may be unavailable. "
            "Check your tunnel provider or install a tunnel plugin."
        )
