"""LLM API Gateway — credential isolation for containers.

Two modes, selected by ``[gateway].litellm_config`` in config.toml:

**LiteLLM mode** (recommended)
    Runs a LiteLLM proxy as a Docker container.  All LLM routing config
    (models, keys, budgets, load balancing) lives in the user-managed
    ``litellm_config.yaml`` — pynchy doesn't translate or duplicate it.

    Pynchy generates an ephemeral master key at startup and passes it to
    the container via ``LITELLM_MASTER_KEY``.  Agent containers authenticate
    with this key, same as the builtin mode.

    LiteLLM serves the native Anthropic Messages API at ``/v1/messages``
    and OpenAI at ``/v1/chat/completions``, so agent containers work
    without URL changes.

**Builtin mode** (fallback)
    Simple aiohttp reverse proxy for single-key setups.  Used when
    ``litellm_config`` is not set.  Reads keys from ``[secrets]``.

Container env vars are set identically for both modes::

    ANTHROPIC_BASE_URL=http://host.docker.internal:<port>
    ANTHROPIC_AUTH_TOKEN=<gateway-key>
    OPENAI_BASE_URL=http://host.docker.internal:<port>
    OPENAI_API_KEY=<gateway-key>

Start with :func:`start_gateway`, access the singleton with :func:`get_gateway`.

Env-var forwarding (LiteLLM mode)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

At startup the gateway scans ``litellm_config.yaml`` for all
``os.environ/VARNAME`` references and forwards matching host env vars
into the Docker container via ``-e``.  The YAML is the single source of
truth — add model entries there, set the corresponding vars in ``.env``,
and pynchy picks them up automatically.

OAuth gotcha
~~~~~~~~~~~~

The Anthropic Messages API does **not** accept OAuth tokens
(``sk-ant-oat01-…``) via ``Authorization: Bearer`` unless the request
also carries the beta header ``anthropic-beta: oauth-2025-04-20``.
Builtin mode handles this automatically.  In LiteLLM mode, use
``extra_headers`` in the litellm config to add the beta header.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from pathlib import Path
from typing import Protocol

import aiohttp
from aiohttp import web

from pynchy.config import get_settings
from pynchy.container_runner._docker import (
    docker_available,
    ensure_image,
    ensure_network,
    run_docker,
)
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Gateway protocol — shared interface for both modes
# ---------------------------------------------------------------------------


class GatewayProto(Protocol):
    port: int
    key: str

    @property
    def base_url(self) -> str: ...
    def has_provider(self, name: str) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# ===========================================================================
# LiteLLM mode — Docker container with PostgreSQL sidecar
# ===========================================================================

_LITELLM_CONTAINER = "pynchy-litellm"
_POSTGRES_CONTAINER = "pynchy-litellm-db"
_NETWORK_NAME = "pynchy-litellm-net"
_LITELLM_INTERNAL_PORT = 4000
_POSTGRES_PORT = 5432
_POSTGRES_DB = "litellm"
_POSTGRES_USER = "litellm"
_HEALTH_TIMEOUT = 90  # seconds; Postgres + LiteLLM migrations need headroom
_HEALTH_POLL_INTERVAL = 1.0
_POSTGRES_HEALTH_TIMEOUT = 30

_SALT_KEY_FILE = "salt.key"


def _load_or_create_persistent_key(path: Path, prefix: str = "") -> str:
    """Read a key from disk, or generate and persist one on first run."""
    if path.exists():
        return path.read_text().strip()
    key = f"{prefix}{secrets.token_urlsafe(32)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    return key


class LiteLLMGateway:
    """Gateway backed by a LiteLLM proxy Docker container.

    Pynchy generates an ephemeral master key and injects it into the
    container via ``LITELLM_MASTER_KEY``.  The litellm_config.yaml should
    reference it::

        general_settings:
          master_key: os.environ/LITELLM_MASTER_KEY

    Or omit ``master_key`` entirely — litellm reads the env var
    automatically.

    A PostgreSQL sidecar container provides persistent storage for
    spend tracking, provider budget caps, and virtual keys.  Both
    containers share a private Docker network.

    Attributes:
        port: Host port mapped to the litellm container.
        key: Ephemeral master key for container authentication.
    """

    def __init__(
        self,
        *,
        config_path: str,
        port: int,
        container_host: str,
        image: str,
        postgres_image: str,
        data_dir: Path,
        master_key: str,
    ) -> None:
        self.port = port
        self.container_host = container_host
        self.key: str = master_key

        self._config_path = Path(config_path).resolve()
        self._image = image
        self._postgres_image = postgres_image
        self._data_dir = data_dir / "litellm"
        self._pg_data_dir = self._data_dir / "postgres"

        self._pg_password = _load_or_create_persistent_key(
            self._data_dir / "pg_password.key",
        )
        self._salt_key = _load_or_create_persistent_key(
            self._data_dir / _SALT_KEY_FILE,
            prefix="sk-salt-",
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.container_host}:{self.port}"

    @property
    def _database_url(self) -> str:
        return (
            f"postgresql://{_POSTGRES_USER}:{self._pg_password}"
            f"@{_POSTGRES_CONTAINER}:{_POSTGRES_PORT}/{_POSTGRES_DB}"
        )

    def has_provider(self, name: str) -> bool:
        # LiteLLM handles provider resolution — always expose both URLs.
        # If a provider isn't configured, litellm returns a clear error.
        return True

    # ------------------------------------------------------------------
    # Env-var forwarding
    # ------------------------------------------------------------------

    # Vars that pynchy sets itself — never forward from host env.
    _GATEWAY_MANAGED_VARS = frozenset({
        "LITELLM_MASTER_KEY",
        "LITELLM_SALT_KEY",
        "DATABASE_URL",
    })

    @staticmethod
    def _collect_yaml_env_refs(config_path: Path) -> list[tuple[str, str]]:
        """Scan litellm config for ``os.environ/`` references and resolve from host env.

        Returns ``(name, value)`` pairs for every referenced var that is
        set on the host.  Gateway-managed vars are excluded.  Missing vars
        produce a warning — LiteLLM will fail at runtime for that entry,
        which surfaces the problem clearly.
        """
        text = config_path.read_text()
        var_names = set(re.findall(r"os\.environ/(\w+)", text))
        var_names -= LiteLLMGateway._GATEWAY_MANAGED_VARS

        resolved: list[tuple[str, str]] = []
        for name in sorted(var_names):
            value = os.environ.get(name)
            if value:
                resolved.append((name, value))
            else:
                logger.warning("YAML references unset env var", var=name)
        return resolved

    # Docker helpers are in _docker.py — imported at module level.

    # ------------------------------------------------------------------
    # PostgreSQL sidecar
    # ------------------------------------------------------------------

    async def _start_postgres(self) -> None:
        """Start the PostgreSQL sidecar and wait for it to accept connections."""
        self._pg_data_dir.mkdir(parents=True, exist_ok=True)
        ensure_image(self._postgres_image)

        run_docker("rm", "-f", _POSTGRES_CONTAINER, check=False)

        logger.info(
            "Starting PostgreSQL sidecar",
            image=self._postgres_image,
            data_dir=str(self._pg_data_dir),
        )

        run_docker(
            "run", "-d",
            "--name", _POSTGRES_CONTAINER,
            "--network", _NETWORK_NAME,
            "-v", f"{self._pg_data_dir}:/var/lib/postgresql/data",
            "-e", f"POSTGRES_USER={_POSTGRES_USER}",
            "-e", f"POSTGRES_PASSWORD={self._pg_password}",
            "-e", f"POSTGRES_DB={_POSTGRES_DB}",
            "--restart", "unless-stopped",
            self._postgres_image,
        )  # fmt: skip

        await self._wait_postgres_healthy()

    async def _wait_postgres_healthy(self) -> None:
        """Poll pg_isready inside the container until Postgres is up."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _POSTGRES_HEALTH_TIMEOUT

        while loop.time() < deadline:
            result = run_docker(
                "exec",
                _POSTGRES_CONTAINER,
                "pg_isready",
                "-U",
                _POSTGRES_USER,
                check=False,
            )
            if result.returncode == 0:
                logger.info("PostgreSQL sidecar ready")
                return

            # Ensure the container is still running
            inspect = run_docker(
                "inspect",
                "-f",
                "{{.State.Running}}",
                _POSTGRES_CONTAINER,
                check=False,
            )
            if inspect.stdout.strip() != "true":
                logs = run_docker(
                    "logs",
                    "--tail",
                    "30",
                    _POSTGRES_CONTAINER,
                    check=False,
                )
                logger.error("PostgreSQL container exited", logs=logs.stdout[-2000:])
                msg = "PostgreSQL container failed to start — check logs above"
                raise RuntimeError(msg)

            await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        msg = f"PostgreSQL did not become ready within {_POSTGRES_HEALTH_TIMEOUT}s"
        raise TimeoutError(msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not docker_available():
            msg = "Docker is required for LiteLLM gateway mode but 'docker' was not found on PATH"
            raise RuntimeError(msg)

        if not self._config_path.exists():
            msg = f"LiteLLM config not found: {self._config_path}"
            raise FileNotFoundError(msg)

        self._data_dir.mkdir(parents=True, exist_ok=True)

        ensure_network(_NETWORK_NAME)
        await self._start_postgres()

        ensure_image(self._image)

        # Remove stale LiteLLM container from previous run
        run_docker("rm", "-f", _LITELLM_CONTAINER, check=False)

        logger.info(
            "Starting LiteLLM proxy container",
            image=self._image,
            config=str(self._config_path),
            port=self.port,
        )

        # Build environment variables
        env_vars = [
            "-e",
            f"LITELLM_MASTER_KEY={self.key}",
            "-e",
            f"LITELLM_SALT_KEY={self._salt_key}",
            "-e",
            f"DATABASE_URL={self._database_url}",
        ]

        # Forward env vars referenced in litellm_config.yaml
        for var_name, value in self._collect_yaml_env_refs(self._config_path):
            env_vars.extend(["-e", f"{var_name}={value}"])

        # Add UI credentials if configured
        s = get_settings()
        if s.gateway.ui_username:
            env_vars.extend(["-e", f"UI_USERNAME={s.gateway.ui_username}"])
        if s.gateway.ui_password:
            env_vars.extend(["-e", f"UI_PASSWORD={s.gateway.ui_password.get_secret_value()}"])

        run_docker(
            "run", "-d",
            "--name", _LITELLM_CONTAINER,
            "--network", _NETWORK_NAME,
            "-p", f"{self.port}:{_LITELLM_INTERNAL_PORT}",
            "-v", f"{self._config_path}:/app/config.yaml:ro",
            "-v", f"{self._data_dir}:/app/data",
            *env_vars,
            "--restart", "unless-stopped",
            self._image,
            "--config", "/app/config.yaml",
            "--port", str(_LITELLM_INTERNAL_PORT),
        )  # fmt: skip

        await self._wait_healthy()

        logger.info(
            "LiteLLM gateway ready",
            port=self.port,
            container_url=self.base_url,
            container=_LITELLM_CONTAINER,
        )

    async def _wait_healthy(self) -> None:
        """Poll litellm's health endpoint until it responds."""
        url = f"http://localhost:{self.port}/health"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _HEALTH_TIMEOUT

        headers = {"Authorization": f"Bearer {self.key}"}

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
        ) as session:
            while loop.time() < deadline:
                try:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            return
                except (aiohttp.ClientError, OSError):
                    pass

                # Check container is still running
                result = run_docker(
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    _LITELLM_CONTAINER,
                    check=False,
                )
                if result.stdout.strip() != "true":
                    logs = run_docker("logs", "--tail", "50", _LITELLM_CONTAINER, check=False)
                    logger.error("LiteLLM container exited", logs=logs.stdout[-2000:])
                    msg = "LiteLLM container failed to start — check logs above"
                    raise RuntimeError(msg)

                await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        msg = f"LiteLLM proxy did not become healthy within {_HEALTH_TIMEOUT}s"
        raise TimeoutError(msg)

    async def stop(self) -> None:
        logger.info("Stopping LiteLLM gateway containers")
        run_docker("stop", "-t", "5", _LITELLM_CONTAINER, check=False)
        run_docker("rm", "-f", _LITELLM_CONTAINER, check=False)
        run_docker("stop", "-t", "5", _POSTGRES_CONTAINER, check=False)
        run_docker("rm", "-f", _POSTGRES_CONTAINER, check=False)
        run_docker("network", "rm", _NETWORK_NAME, check=False)
        logger.info("LiteLLM gateway stopped")


# ===========================================================================
# Builtin mode — aiohttp reverse proxy (single-key fallback)
# ===========================================================================

_ANTHROPIC_BASE = "https://api.anthropic.com"
_OPENAI_BASE = "https://api.openai.com"
_ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"

_STRIP_REQUEST_HEADERS = frozenset({"authorization", "x-api-key", "host", "content-length"})
_STRIP_RESPONSE_HEADERS = frozenset(
    {"transfer-encoding", "content-encoding", "connection", "keep-alive"}
)


def _resolve_provider(path: str) -> tuple[str, str] | None:
    """Map request path to ``(provider_name, upstream_url)``."""
    if path.startswith("/v1/messages"):
        return "anthropic", f"{_ANTHROPIC_BASE}{path}"
    if path.startswith("/v1/"):
        return "openai", f"{_OPENAI_BASE}{path}"
    return None


class BuiltinGateway:
    """Simple aiohttp reverse proxy for single-key setups.

    Used when ``litellm_config`` is not set.  Reads keys from
    ``[secrets]`` in config.toml.
    """

    def __init__(self, *, port: int, host: str, container_host: str) -> None:
        self.port = port
        self.host = host
        self.container_host = container_host
        self.key: str = f"gw-{secrets.token_urlsafe(32)}"

        self._credentials: dict[str, dict[str, str]] = {}
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.container_host}:{self.port}"

    def has_provider(self, name: str) -> bool:
        return name in self._credentials

    # ------------------------------------------------------------------
    # Credential discovery
    # ------------------------------------------------------------------

    def _discover_credentials(self) -> None:
        from pynchy.container_runner._credentials import _read_oauth_token

        s = get_settings()
        providers: dict[str, dict[str, str]] = {}

        if s.secrets.anthropic_api_key:
            providers["anthropic"] = {
                "type": "api_key",
                "value": s.secrets.anthropic_api_key.get_secret_value(),
            }
        elif s.secrets.claude_code_oauth_token:
            providers["anthropic"] = {
                "type": "oauth",
                "value": s.secrets.claude_code_oauth_token.get_secret_value(),
            }
        else:
            token = _read_oauth_token()
            if token:
                providers["anthropic"] = {"type": "oauth", "value": token}

        if s.secrets.openai_api_key:
            providers["openai"] = {
                "type": "api_key",
                "value": s.secrets.openai_api_key.get_secret_value(),
            }

        self._credentials = providers
        auth_types = {name: cred["type"] for name, cred in providers.items()}
        logger.info(
            "Gateway credentials discovered",
            providers=list(providers.keys()) or ["none"],
            auth_types=auth_types or None,
        )

    # ------------------------------------------------------------------
    # Auth & proxying
    # ------------------------------------------------------------------

    def _validate_auth(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        api_key = request.headers.get("X-Api-Key", "")
        return auth == f"Bearer {self.key}" or api_key == self.key

    def _build_upstream_headers(self, request: web.Request, provider: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() not in _STRIP_REQUEST_HEADERS:
                headers[key] = value

        creds = self._credentials[provider]
        if provider == "anthropic":
            if creds["type"] == "api_key":
                headers["x-api-key"] = creds["value"]
            else:
                headers["Authorization"] = f"Bearer {creds['value']}"
                existing_beta = headers.get("anthropic-beta", "")
                if _ANTHROPIC_OAUTH_BETA not in existing_beta:
                    headers["anthropic-beta"] = (
                        f"{existing_beta},{_ANTHROPIC_OAUTH_BETA}"
                        if existing_beta
                        else _ANTHROPIC_OAUTH_BETA
                    )
        elif provider == "openai":
            headers["Authorization"] = f"Bearer {creds['value']}"

        return headers

    async def _proxy_handler(self, request: web.Request) -> web.StreamResponse:
        path = f"/{request.match_info.get('path', '')}"

        if not self._validate_auth(request):
            return web.Response(status=401, text="Unauthorized")

        result = _resolve_provider(path)
        if result is None:
            return web.Response(status=404, text="Unknown API path")

        provider, upstream_url = result
        if provider not in self._credentials:
            logger.warning("Gateway request for unconfigured provider", provider=provider)
            return web.Response(
                status=503,
                text=f"No credentials configured for {provider}",
            )

        headers = self._build_upstream_headers(request, provider)
        body = await request.read()

        assert self._session is not None
        try:
            async with self._session.request(
                method=request.method,
                url=upstream_url,
                headers=headers,
                data=body,
            ) as upstream:
                resp_headers: dict[str, str] = {}
                for key, value in upstream.headers.items():
                    if key.lower() not in _STRIP_RESPONSE_HEADERS:
                        resp_headers[key] = value

                response = web.StreamResponse(
                    status=upstream.status,
                    headers=resp_headers,
                )
                await response.prepare(request)

                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)

                await response.write_eof()
                return response
        except aiohttp.ClientError as exc:
            logger.error("Gateway upstream error", provider=provider, err=str(exc))
            return web.Response(status=502, text=f"Gateway error: {type(exc).__name__}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._discover_credentials()

        if not self._credentials:
            logger.warning(
                "Gateway has no LLM credentials — containers will fail to authenticate. "
                "Configure [secrets] in config.toml or authenticate via 'claude' CLI."
            )

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None),
        )

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._proxy_handler)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

        logger.info(
            "Builtin LLM gateway listening",
            port=self.port,
            container_url=self.base_url,
            providers=list(self._credentials.keys()),
        )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Builtin gateway stopped")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_gateway: LiteLLMGateway | BuiltinGateway | None = None


def get_gateway() -> LiteLLMGateway | BuiltinGateway | None:
    """Return the active gateway, or ``None`` if not started."""
    return _gateway


async def start_gateway() -> LiteLLMGateway | BuiltinGateway:
    """Start the appropriate gateway based on config. Returns the instance."""
    global _gateway
    s = get_settings()

    if s.gateway.litellm_config:
        logger.info("Using LiteLLM gateway mode", config=s.gateway.litellm_config)
        if not s.gateway.master_key:
            raise ValueError(
                "GATEWAY__MASTER_KEY is required when using LiteLLM mode. Set it in .env."
            )
        _gateway = LiteLLMGateway(
            config_path=s.gateway.litellm_config,
            port=s.gateway.port,
            container_host=s.gateway.container_host,
            image=s.gateway.litellm_image,
            postgres_image=s.gateway.postgres_image,
            data_dir=s.data_dir,
            master_key=s.gateway.master_key.get_secret_value(),
        )
    else:
        logger.info("Using builtin gateway mode (no litellm_config set)")
        _gateway = BuiltinGateway(
            port=s.gateway.port,
            host=s.gateway.host,
            container_host=s.gateway.container_host,
        )

    await _gateway.start()

    # Sync MCP state to LiteLLM after gateway is ready (LiteLLM mode only)
    if isinstance(_gateway, LiteLLMGateway) and s.mcp_servers:
        from pynchy.container_runner.mcp_manager import McpManager, set_mcp_manager

        mcp_mgr = McpManager(s, _gateway)
        set_mcp_manager(mcp_mgr)
        await mcp_mgr.sync()

    return _gateway


async def stop_gateway() -> None:
    """Stop the gateway if running."""
    global _gateway

    # Stop MCP containers before stopping the gateway
    from pynchy.container_runner.mcp_manager import get_mcp_manager, set_mcp_manager

    mcp_mgr = get_mcp_manager()
    if mcp_mgr is not None:
        await mcp_mgr.stop_all()
        set_mcp_manager(None)

    if _gateway is not None:
        await _gateway.stop()
        _gateway = None
