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

**Builtin mode** (fallback)  TODO - delete this section once LiteLLM mode is fully tested
    Simple aiohttp reverse proxy for single-key setups.  Used when
    ``litellm_config`` is not set.  Reads keys from ``[secrets]``.

Container env vars are set identically for both modes::

    ANTHROPIC_BASE_URL=http://host.docker.internal:<port>
    ANTHROPIC_AUTH_TOKEN=<gateway-key>
    OPENAI_BASE_URL=http://host.docker.internal:<port>
    OPENAI_API_KEY=<gateway-key>

Start with :func:`start_gateway`, access the singleton with :func:`get_gateway`.

OAuth gotcha (builtin mode only)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Anthropic Messages API does **not** accept OAuth tokens
(``sk-ant-oat01-…``) via ``Authorization: Bearer`` unless the request
also carries the beta header ``anthropic-beta: oauth-2025-04-20``.
This is handled automatically in builtin mode.  In LiteLLM mode,
use API keys (``sk-ant-api03-…``) instead of OAuth tokens.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

import aiohttp
from aiohttp import web

from pynchy.config import get_settings
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
# LiteLLM mode — Docker container
# ===========================================================================

_LITELLM_CONTAINER = "pynchy-litellm"
_LITELLM_INTERNAL_PORT = 4000
_HEALTH_TIMEOUT = 60  # seconds to wait for litellm to become healthy
_HEALTH_POLL_INTERVAL = 1.0


class LiteLLMGateway:
    """Gateway backed by a LiteLLM proxy Docker container.

    Pynchy generates an ephemeral master key and injects it into the
    container via ``LITELLM_MASTER_KEY``.  The litellm_config.yaml should
    reference it::

        general_settings:
          master_key: os.environ/LITELLM_MASTER_KEY

    Or omit ``master_key`` entirely — litellm reads the env var
    automatically.

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
        data_dir: Path,
    ) -> None:
        self.port = port
        self.container_host = container_host
        self.key: str = f"sk-pynchy-{secrets.token_urlsafe(32)}"

        self._config_path = Path(config_path).resolve()
        self._image = image
        self._data_dir = data_dir / "litellm"

    @property
    def base_url(self) -> str:
        return f"http://{self.container_host}:{self.port}"

    def has_provider(self, name: str) -> bool:
        # LiteLLM handles provider resolution — always expose both URLs.
        # If a provider isn't configured, litellm returns a clear error.
        return True

    # ------------------------------------------------------------------
    # Docker helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _docker_available() -> bool:
        return shutil.which("docker") is not None

    @staticmethod
    def _run_docker(
        *args: str,
        check: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_image(self) -> None:
        """Pull the LiteLLM image if not already present."""
        result = self._run_docker(
            "image",
            "inspect",
            self._image,
            check=False,
        )
        if result.returncode == 0:
            return

        logger.info("Pulling LiteLLM image (first run may take a minute)", image=self._image)
        self._run_docker("pull", self._image, timeout=300)
        logger.info("LiteLLM image pulled", image=self._image)

    async def start(self) -> None:
        if not self._docker_available():
            msg = "Docker is required for LiteLLM gateway mode but 'docker' was not found on PATH"
            raise RuntimeError(msg)

        if not self._config_path.exists():
            msg = f"LiteLLM config not found: {self._config_path}"
            raise FileNotFoundError(msg)

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_image()

        # Remove stale container from previous run
        self._run_docker("rm", "-f", _LITELLM_CONTAINER, check=False)

        logger.info(
            "Starting LiteLLM proxy container",
            image=self._image,
            config=str(self._config_path),
            port=self.port,
        )

        self._run_docker(
            "run", "-d",
            "--name", _LITELLM_CONTAINER,
            "-p", f"{self.port}:{_LITELLM_INTERNAL_PORT}",
            "-v", f"{self._config_path}:/app/config.yaml:ro",
            "-v", f"{self._data_dir}:/app/data",
            "-e", f"LITELLM_MASTER_KEY={self.key}",
            "-e", "DATABASE_URL=sqlite:///app/data/litellm.db",
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
        deadline = asyncio.get_event_loop().time() + _HEALTH_TIMEOUT

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
        ) as session:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            return
                except (aiohttp.ClientError, OSError):
                    pass

                # Check container is still running
                result = self._run_docker(
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    _LITELLM_CONTAINER,
                    check=False,
                )
                if result.stdout.strip() != "true":
                    logs = self._run_docker("logs", "--tail", "50", _LITELLM_CONTAINER, check=False)
                    logger.error("LiteLLM container exited", logs=logs.stdout[-2000:])
                    msg = "LiteLLM container failed to start — check logs above"
                    raise RuntimeError(msg)

                await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        msg = f"LiteLLM proxy did not become healthy within {_HEALTH_TIMEOUT}s"
        raise TimeoutError(msg)

    async def stop(self) -> None:
        logger.info("Stopping LiteLLM proxy container")
        self._run_docker("stop", "-t", "5", _LITELLM_CONTAINER, check=False)
        self._run_docker("rm", "-f", _LITELLM_CONTAINER, check=False)
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
        _gateway = LiteLLMGateway(
            config_path=s.gateway.litellm_config,
            port=s.gateway.port,
            container_host=s.gateway.container_host,
            image=s.gateway.litellm_image,
            data_dir=s.data_dir,
        )
    else:
        logger.info("Using builtin gateway mode (no litellm_config set)")
        _gateway = BuiltinGateway(
            port=s.gateway.port,
            host=s.gateway.host,
            container_host=s.gateway.container_host,
        )

    await _gateway.start()
    return _gateway


async def stop_gateway() -> None:
    """Stop the gateway if running."""
    global _gateway
    if _gateway is not None:
        await _gateway.stop()
        _gateway = None
