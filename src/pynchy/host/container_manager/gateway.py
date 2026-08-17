"""LLM API Gateway — credential isolation for containers.

Normal startup convention-wires the personalized LiteLLM source. Component
callers can also select built-in mode:

**LiteLLM mode** (recommended)
    Runs a LiteLLM proxy as a Docker container.  All LLM routing config
    (models, keys, budgets, load balancing) lives in the user-managed
    ``data/personalization/litellm.yaml`` — Pynchy prepares a runtime copy.

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

import asyncio
from collections.abc import (  # noqa: TC003 - beartype resolves gateway runtime annotations.
    Callable,
    Mapping,
)
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - beartype resolves gateway runtime annotations.
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pynchy.plugins.api import (  # beartype resolves the collector return annotation at runtime.
    McpServerConfig,
)

if TYPE_CHECKING:
    import pluggy

    from pynchy.redaction import GatewayRedactionPosture
from pynchy.host.container_manager.gateway_builtin import (
    BuiltinGateway,
    BuiltinGatewayCredentials,
)
from pynchy.host.container_manager.gateway_litellm import (
    LiteLLMGateway,
    LiteLLMGatewayCredentials,
)
from pynchy.logger import logger
from pynchy.plugins.api import McpServerSpec
from pynchy.workspace.api import (
    ServiceTrustConfig,  # noqa: TC001 - beartype resolves the collector return annotation at runtime.
)

_GATEWAY_MASTER_KEY_REQUIRED_ERROR = (
    "GATEWAY__MASTER_KEY is required when using LiteLLM mode. Set it in .env."
)

# Public gateway API re-exported from this module
__all__ = [
    "BuiltinGateway",
    "GatewayProto",
    "LiteLLMGateway",
    "get_gateway",
    "recover_gateway_if_unhealthy",
    "resolve_container_host",
    "start_gateway",
    "stop_gateway",
    "supervise_gateway",
]

_DEFAULT_CONTAINER_HOST = "host.docker.internal"
_APPLE_CONTAINER_HOST = "192.168.64.1"
_SUPERVISOR_INTERVAL_SECONDS = 5
_SUPERVISOR_MAX_BACKOFF_SECONDS = 60
_apple_container_runtime = False


class _SecretValue(Protocol):
    def get_secret_value(self) -> str: ...


class _GatewayConfig(Protocol):
    container_host: str
    litellm_config: str | None
    managed: bool
    master_key: _SecretValue | None
    port: int
    litellm_image: str
    postgres_image: str
    ui_username: str | None
    ui_password: _SecretValue | None
    host: str


class _AgentConfig(Protocol):
    default_core: str


class _SecretsConfig(Protocol):
    anthropic_api_key: _SecretValue | None
    openai_api_key: _SecretValue | None


class GatewaySettings(Protocol):
    gateway: _GatewayConfig
    data_dir: Path
    agent: _AgentConfig
    secrets: _SecretsConfig
    tools: Mapping[str, object]

    def configured_agent_models(self) -> tuple[str, ...]: ...


_get_settings: Callable[[], GatewaySettings] | None = None


def configure_gateway_runtime(
    *,
    is_apple_container: bool,
    get_settings: Callable[[], GatewaySettings] | None = None,
) -> None:
    """Select container-network behavior at host composition."""
    global _apple_container_runtime, _get_settings  # noqa: PLW0603 - one host process owns one container-network mode.
    _apple_container_runtime = is_apple_container
    if get_settings is not None:
        _get_settings = get_settings


def _configured_settings() -> GatewaySettings:
    if _get_settings is None:
        raise RuntimeError("Gateway configuration has not been composed")
    return _get_settings()


def get_settings() -> GatewaySettings:
    """Return the composed gateway configuration."""
    return _configured_settings()


def _is_mcp_tool(tool: object) -> bool:
    return getattr(tool, "type", None) == "mcp"


# ---------------------------------------------------------------------------
# Gateway protocol — shared interface for both modes
# ---------------------------------------------------------------------------


@runtime_checkable
class GatewayProto(Protocol):
    port: int
    key: str

    @property
    def base_url(self) -> str: ...
    @property
    def redaction_posture(self) -> GatewayRedactionPosture: ...
    def has_provider(self, name: str) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


@dataclass
class _GatewayState:
    gateway: LiteLLMGateway | BuiltinGateway | None = None


_state = _GatewayState()


def get_gateway() -> LiteLLMGateway | BuiltinGateway | None:
    """Return the active gateway, or ``None`` if not started."""
    return _state.gateway


def _set_gateway(gateway: LiteLLMGateway | BuiltinGateway | None) -> None:
    _state.gateway = gateway


def resolve_container_host(container_host: str) -> str:
    """Return the gateway host that agent containers can actually reach."""
    if container_host != _DEFAULT_CONTAINER_HOST:
        return container_host

    if _apple_container_runtime:
        # Apple Container does not provide Docker's host.docker.internal DNS
        # name; the host gateway for its default VM bridge is 192.168.64.1.
        return _APPLE_CONTAINER_HOST
    return container_host


def _required_litellm_models(
    *,
    agent_core: str,
    models: tuple[str, ...],
) -> tuple[str, ...]:
    """Return LiteLLM model aliases that the active core can request directly."""
    if agent_core in {"claude-cli", "codex", "openai"}:
        return models
    return ()


def _required_litellm_response_models(
    *,
    agent_core: str,
    models: tuple[str, ...],
    cop_model: str,
    cop_wire_api: str,
) -> tuple[str, ...]:
    """Return selected model aliases whose configured routes must support Responses."""
    # Claude CLI uses LiteLLM's Messages API, while OpenAI and Codex send
    # Responses requests through their configured model aliases.
    direct_models = models if agent_core in {"openai", "codex"} else ()
    cop_models = (cop_model,) if cop_wire_api == "responses" else ()
    return tuple(dict.fromkeys((*direct_models, *cop_models)))


def collect_plugin_mcp_servers(
    plugin_manager: pluggy.PluginManager | None,
) -> tuple[dict[str, McpServerConfig], dict[str, ServiceTrustConfig]]:
    """Collect MCP server specs from plugins.

    Returns ``(server_configs, trust_defaults)``.

    Trust metadata is returned separately so callers can flow it into the
    proxy's trust map.
    """
    if plugin_manager is None:
        return {}, {}

    result: dict[str, McpServerConfig] = {}
    trust_defaults: dict[str, ServiceTrustConfig] = {}

    for contribution in plugin_manager.hook.pynchy_mcp_server_spec():
        if not isinstance(contribution, tuple):
            logger.warning(
                "Ignoring invalid MCP server plugin contribution",
                result_type=type(contribution).__name__,
            )
            continue
        for spec in contribution:
            if not isinstance(spec, McpServerSpec):
                logger.warning(
                    "Ignoring invalid MCP server plugin spec",
                    spec_type=type(spec).__name__,
                )
                continue
            result[spec.name] = spec.config
            if spec.trust is not None:
                trust_defaults[spec.name] = spec.trust
            logger.info("Collected plugin MCP server spec", name=spec.name)

    return result, trust_defaults


async def start_gateway(
    plugin_manager: pluggy.PluginManager | None = None,
) -> LiteLLMGateway | BuiltinGateway:
    """Start the appropriate gateway based on config. Returns the instance.

    *plugin_manager* is optional — when provided, plugin-supplied MCP server
    specs (via ``pynchy_mcp_server_spec``) are merged into the MCP manager.
    """
    s = get_settings()
    container_host = resolve_container_host(s.gateway.container_host)

    if s.gateway.litellm_config:
        logger.info("Using LiteLLM gateway mode", config=s.gateway.litellm_config)
        if not s.gateway.master_key:
            raise ValueError(_GATEWAY_MASTER_KEY_REQUIRED_ERROR)
        from pynchy.host.container_manager.security.cop_client import (  # noqa: PLC0415 - Cop transport composition resolves before gateway startup.
            get_cop_gateway_config,
        )

        cop_model, cop_wire_api = get_cop_gateway_config()
        gateway: LiteLLMGateway | BuiltinGateway = LiteLLMGateway(
            config_path=s.gateway.litellm_config,
            port=s.gateway.port,
            container_host=container_host,
            image=s.gateway.litellm_image,
            postgres_image=s.gateway.postgres_image,
            data_dir=s.data_dir,
            master_key=s.gateway.master_key.get_secret_value(),
            managed=s.gateway.managed,
            required_models=_required_litellm_models(
                agent_core=s.agent.default_core,
                models=s.configured_agent_models(),
            ),
            required_response_models=_required_litellm_response_models(
                agent_core=s.agent.default_core,
                models=s.configured_agent_models(),
                cop_model=cop_model,
                cop_wire_api=cop_wire_api,
            ),
            ui_credentials=LiteLLMGatewayCredentials(
                ui_username=s.gateway.ui_username,
                ui_password=(
                    s.gateway.ui_password.get_secret_value() if s.gateway.ui_password else None
                ),
            ),
        )
    else:
        logger.info("Using builtin gateway mode (no litellm_config set)")
        gateway = BuiltinGateway(
            port=s.gateway.port,
            host=s.gateway.host,
            container_host=container_host,
            credentials=BuiltinGatewayCredentials(
                anthropic_api_key=(
                    s.secrets.anthropic_api_key.get_secret_value()
                    if s.secrets.anthropic_api_key
                    else None
                ),
                openai_api_key=(
                    s.secrets.openai_api_key.get_secret_value()
                    if s.secrets.openai_api_key
                    else None
                ),
            ),
        )

    _set_gateway(gateway)
    await gateway.start()

    # Sync MCP state to LiteLLM after gateway is ready (LiteLLM mode only).
    # Collect plugin-provided MCP server specs and merge with personalized settings.
    plugin_mcp_servers, plugin_trust_defaults = collect_plugin_mcp_servers(plugin_manager)
    has_servers = any(_is_mcp_tool(tool) for tool in s.tools.values()) or plugin_mcp_servers
    if isinstance(gateway, LiteLLMGateway) and has_servers:
        from pynchy.host.container_manager.mcp.manager import (  # noqa: PLC0415 - defer MCP manager setup until LiteLLM gateway is ready
            McpManager,
            set_mcp_manager,
        )

        mcp_mgr = McpManager(
            s,
            gateway,
            plugin_mcp_servers=plugin_mcp_servers,
            plugin_trust_defaults=plugin_trust_defaults,
        )
        set_mcp_manager(mcp_mgr)
        await mcp_mgr.sync()

    return gateway


async def stop_gateway() -> None:
    """Stop the gateway if running."""

    # Stop MCP containers before stopping the gateway
    from pynchy.host.container_manager.mcp.manager import (  # noqa: PLC0415 - defer MCP manager import until shutdown actually needs it
        get_mcp_manager,
        set_mcp_manager,
    )

    cleanup_error: Exception | None = None
    if (mcp_mgr := get_mcp_manager()) is not None:
        try:
            await mcp_mgr.stop_all()
        except Exception as exc:  # noqa: BLE001  # allow: exception-handling - continue independent cleanup.
            cleanup_error = exc
        else:
            set_mcp_manager(None)

    if (gateway := get_gateway()) is not None:
        try:
            await gateway.stop()
        except Exception:  # allow: exception-handling - retain failed handle for retry.
            if cleanup_error is None:
                raise
        else:
            _set_gateway(None)
    if cleanup_error is not None:
        raise cleanup_error


async def stop_gateway_after_startup_failure() -> None:
    """Stop gateway sidecars without masking the startup failure."""
    try:
        await stop_gateway()
    except Exception:  # noqa: BLE001 - preserve the error that aborted startup.
        logger.exception("Gateway cleanup failed during startup rollback")


async def recover_gateway_if_unhealthy() -> bool:
    """Restart a lost LiteLLM gateway and restore its MCP registrations."""
    gateway = get_gateway()
    if not isinstance(gateway, LiteLLMGateway) or await gateway.is_ready():
        return False

    logger.warning("LiteLLM gateway unavailable; restarting owned sidecars")
    await gateway.start()

    from pynchy.host.container_manager.mcp.manager import (  # noqa: PLC0415 - MCP runtime exists only after LiteLLM startup.
        get_mcp_manager,
    )

    if mcp_manager := get_mcp_manager():
        await mcp_manager.sync()
    logger.info("LiteLLM gateway recovered")
    return True


async def supervise_gateway() -> None:
    """Keep the service-owned LiteLLM sidecars alive after external removal."""
    backoff_seconds = 1
    while True:
        await asyncio.sleep(_SUPERVISOR_INTERVAL_SECONDS)
        try:
            await recover_gateway_if_unhealthy()
        except Exception:  # noqa: BLE001 - recovery must keep retrying after a transient runtime failure.
            logger.exception("LiteLLM gateway recovery failed", retry_seconds=backoff_seconds)
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, _SUPERVISOR_MAX_BACKOFF_SECONDS)
        else:
            backoff_seconds = 1
