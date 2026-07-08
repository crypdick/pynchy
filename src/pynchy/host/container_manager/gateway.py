"""LLM API Gateway — credential isolation for containers.

Two modes, selected by ``[gateway].litellm_config`` in config.toml:

**LiteLLM mode** (recommended)
    Runs a LiteLLM proxy as a Docker container.  All LLM routing config
    (models, keys, budgets, load balancing) lives in the user-managed
    ``litellm_config.yaml`` — pynchy doesn't translate or duplicate it.

**Builtin mode** (default when LiteLLM is unconfigured)
    Simple aiohttp reverse proxy for single-key setups.  Used when
    ``litellm_config`` is not set.  Reads keys from ``[secrets]``.

Container env vars are set identically for both modes::

    ANTHROPIC_BASE_URL=http://<container-reachable-host>:<port>
    ANTHROPIC_AUTH_TOKEN=<gateway-key>
    OPENAI_BASE_URL=http://<container-reachable-host>:<port>
    OPENAI_API_KEY=<gateway-key>

``host.docker.internal`` remains the config default.  Runtime-specific
resolution maps that default to the host address each container runtime
actually supports.

Start with :func:`start_gateway`, access the singleton with :func:`get_gateway`.

Implementation lives in:
- ``_gateway_litellm.py`` — LiteLLM Docker proxy + PostgreSQL sidecar
- ``_gateway_builtin.py`` — aiohttp reverse proxy for single-key setups
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pynchy.config import get_settings

if TYPE_CHECKING:
    import pluggy
from pynchy.host.container_manager.gateway_builtin import BuiltinGateway
from pynchy.host.container_manager.gateway_litellm import (
    LiteLLMGateway,
    _load_or_create_persistent_key,
)
from pynchy.logger import logger
from pynchy.types import ServiceTrustConfig

# Public gateway API re-exported from this module
__all__ = [
    "BuiltinGateway",
    "GatewayProto",
    "LiteLLMGateway",
    "_load_or_create_persistent_key",
    "get_gateway",
    "resolve_container_host",
    "start_gateway",
    "stop_gateway",
]

_DEFAULT_CONTAINER_HOST = "host.docker.internal"
_APPLE_CONTAINER_HOST = "192.168.64.1"


# ---------------------------------------------------------------------------
# Gateway protocol — shared interface for both modes
# ---------------------------------------------------------------------------


@runtime_checkable
class GatewayProto(Protocol):
    port: int
    key: str

    @property
    def base_url(self) -> str: ...
    def has_provider(self, name: str) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_gateway: LiteLLMGateway | BuiltinGateway | None = None


def get_gateway() -> LiteLLMGateway | BuiltinGateway | None:
    """Return the active gateway, or ``None`` if not started."""
    return _gateway


def resolve_container_host(container_host: str) -> str:
    """Return the gateway host that agent containers can actually reach."""
    if container_host != _DEFAULT_CONTAINER_HOST:
        return container_host

    from pynchy.plugins.runtimes.detection import get_runtime

    try:
        runtime_name = get_runtime().name
    except RuntimeError:
        return container_host

    if runtime_name == "apple":
        # Apple Container does not provide Docker's host.docker.internal DNS
        # name; the host gateway for its default VM bridge is 192.168.64.1.
        return _APPLE_CONTAINER_HOST
    return container_host


def _required_litellm_models(
    *,
    agent_core: str,
    model: str | None,
    fallback_model: str | None,
) -> tuple[str, ...]:
    """Return LiteLLM model aliases that the active core can request directly."""
    if agent_core == "codex":
        return (model,) if model else ()
    if agent_core == "openai":
        return tuple(m for m in (model, fallback_model) if m)
    return ()


def collect_plugin_mcp_servers(
    plugin_manager: pluggy.PluginManager | None,
) -> tuple[dict[str, Any], dict[str, ServiceTrustConfig]]:
    """Collect MCP server specs from plugins.

    Returns ``(server_configs, trust_defaults)``.

    The ``trust`` key is popped from each spec *before* ``McpServerConfig``
    validation because that model uses ``extra="forbid"``.  Trust metadata
    is returned separately so callers can flow it into the proxy's trust map.
    """
    if plugin_manager is None:
        return {}, {}

    from pynchy.config.mcp import McpServerConfig

    result: dict[str, McpServerConfig] = {}
    trust_defaults: dict[str, ServiceTrustConfig] = {}

    for raw in plugin_manager.hook.pynchy_mcp_server_spec():
        # Plugins can return a single dict or a list of dicts
        specs = raw if isinstance(raw, list) else [raw]
        for spec in specs:
            if not isinstance(spec, dict):
                logger.warning(
                    "Ignoring invalid MCP server plugin spec",
                    spec_type=type(spec).__name__,
                )
                continue

            name = spec.pop("name", None)
            if not isinstance(name, str):
                logger.warning("Ignoring MCP server plugin spec without name", spec=spec)
                continue

            # Extract trust before McpServerConfig validation (extra="forbid")
            trust = spec.pop("trust", None)

            try:
                config = McpServerConfig.model_validate({"type": "script", **spec})
            except (ValueError, TypeError) as exc:
                logger.warning("Invalid MCP server config from plugin", name=name, err=str(exc))
                continue

            result[name] = config

            if trust and isinstance(trust, dict):
                trust_defaults[name] = ServiceTrustConfig(**trust)

            logger.info("Collected plugin MCP server spec", name=name)

    return result, trust_defaults


async def start_gateway(
    plugin_manager: pluggy.PluginManager | None = None,
) -> LiteLLMGateway | BuiltinGateway:
    """Start the appropriate gateway based on config. Returns the instance.

    *plugin_manager* is optional — when provided, plugin-supplied MCP server
    specs (via ``pynchy_mcp_server_spec``) are merged into the MCP manager.
    """
    global _gateway
    s = get_settings()
    container_host = resolve_container_host(s.gateway.container_host)

    if s.gateway.litellm_config:
        logger.info("Using LiteLLM gateway mode", config=s.gateway.litellm_config)
        if not s.gateway.master_key:
            raise ValueError(
                "GATEWAY__MASTER_KEY is required when using LiteLLM mode. Set it in .env."
            )
        _gateway = LiteLLMGateway(
            config_path=s.gateway.litellm_config,
            port=s.gateway.port,
            container_host=container_host,
            image=s.gateway.litellm_image,
            postgres_image=s.gateway.postgres_image,
            data_dir=s.data_dir,
            master_key=s.gateway.master_key.get_secret_value(),
            required_models=_required_litellm_models(
                agent_core=s.agent.core,
                model=s.agent.model,
                fallback_model=s.agent.fallback_model,
            ),
        )
    else:
        logger.info("Using builtin gateway mode (no litellm_config set)")
        _gateway = BuiltinGateway(
            port=s.gateway.port,
            host=s.gateway.host,
            container_host=container_host,
        )

    await _gateway.start()

    # Sync MCP state to LiteLLM after gateway is ready (LiteLLM mode only).
    # Collect plugin-provided MCP server specs and merge with config.toml.
    plugin_mcp_servers, plugin_trust_defaults = collect_plugin_mcp_servers(plugin_manager)
    has_servers = s.mcp_servers or s.mcp_server_instances or plugin_mcp_servers
    if isinstance(_gateway, LiteLLMGateway) and has_servers:
        from pynchy.host.container_manager.mcp.manager import McpManager, set_mcp_manager

        mcp_mgr = McpManager(
            s,
            _gateway,
            plugin_mcp_servers=plugin_mcp_servers,
            plugin_trust_defaults=plugin_trust_defaults,
        )
        set_mcp_manager(mcp_mgr)
        await mcp_mgr.sync()

    return _gateway


async def stop_gateway() -> None:
    """Stop the gateway if running."""
    global _gateway

    # Stop MCP containers before stopping the gateway
    from pynchy.host.container_manager.mcp.manager import get_mcp_manager, set_mcp_manager

    mcp_mgr = get_mcp_manager()
    if mcp_mgr is not None:
        await mcp_mgr.stop_all()
        set_mcp_manager(None)

    if _gateway is not None:
        await _gateway.stop()
        _gateway = None
