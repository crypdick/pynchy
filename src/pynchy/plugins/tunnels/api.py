"""Tunnel provider detection with plugin-extensible providers.

Tailscale is built in. Additional tunnel providers (Cloudflare Tunnel,
WireGuard, etc.) can be provided by plugins via ``pynchy_tunnel``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeGuard, runtime_checkable

from pynchy.logger import logger

if TYPE_CHECKING:
    import pluggy

__all__ = ["TunnelProvider", "check_tunnels"]


@runtime_checkable
class TunnelProvider(Protocol):
    """Tunnel provider contract implemented by built-ins and plugins."""

    name: str

    def is_available(self) -> bool: ...

    def is_connected(self) -> bool: ...

    def status_summary(self) -> str: ...


def _is_valid_tunnel_provider(candidate: object) -> TypeGuard[TunnelProvider]:
    return all(
        [
            hasattr(candidate, "name"),
            callable(getattr(candidate, "is_available", None)),
            callable(getattr(candidate, "is_connected", None)),
            callable(getattr(candidate, "status_summary", None)),
        ]
    )


def _check_tunnel_provider(t: TunnelProvider) -> str | None:
    try:
        if not t.is_available():
            logger.info("Tunnel not available on this host", tunnel=t.name)
            return None
        if t.is_connected():
            logger.info("Tunnel connected", tunnel=t.name, status=t.status_summary())
            return t.name
        logger.warning(
            "Tunnel not connected",
            tunnel=t.name,
            status=t.status_summary(),
        )
    except Exception as exc:  # noqa: BLE001 - tunnel provider checks are best-effort plugin isolation.
        logger.warning("Tunnel check failed", tunnel=t.name, err=str(exc))
        return None
    else:
        return None


def check_tunnels(pm: pluggy.PluginManager) -> None:
    """Check all registered tunnel providers.

    Non-fatal: logs warnings but never raises.
    """
    try:
        candidates = pm.hook.pynchy_tunnel()
    except Exception:  # noqa: BLE001 - one plugin must not break tunnel discovery.
        logger.exception("Failed to resolve tunnel plugins")
        return
    tunnels = [candidate for candidate in candidates if _is_valid_tunnel_provider(candidate)]

    if not tunnels:
        logger.info("No tunnel plugins registered")
        return

    for t in tunnels:
        _check_tunnel_provider(t)
