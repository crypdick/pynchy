"""LLM API Gateway — reverse proxy for credential isolation.

Runs an aiohttp server that proxies container LLM API calls to real
providers. Containers never see real API keys; they authenticate to the
gateway with a per-session ephemeral key.

    Container ──[gateway key]──► Gateway ──[real API key]──► Provider

The gateway:
1. Validates requests using a per-session key (``gw-…``)
2. Routes to the correct provider based on request path
3. Injects real API credentials (never visible to containers)
4. Streams responses back transparently

Containers receive env vars like::

    ANTHROPIC_BASE_URL=http://host.docker.internal:4010
    ANTHROPIC_AUTH_TOKEN=gw-<random>
    OPENAI_BASE_URL=http://host.docker.internal:4010
    OPENAI_API_KEY=gw-<random>

Start with :func:`start_gateway`, access the singleton with :func:`get_gateway`.
"""

from __future__ import annotations

import secrets

import aiohttp
from aiohttp import web

from pynchy.config import get_settings
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

_ANTHROPIC_BASE = "https://api.anthropic.com"
_OPENAI_BASE = "https://api.openai.com"

# Headers stripped from the inbound request (replaced with real credentials)
_STRIP_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "host",
        "content-length",
    }
)

# Headers stripped from the upstream response (avoid hop-by-hop leaks)
_STRIP_RESPONSE_HEADERS = frozenset(
    {
        "transfer-encoding",
        "content-encoding",
        "connection",
        "keep-alive",
    }
)


def _resolve_provider(path: str) -> tuple[str, str] | None:
    """Map request path to ``(provider_name, upstream_url)``."""
    if path.startswith("/v1/messages"):
        return "anthropic", f"{_ANTHROPIC_BASE}{path}"
    if path.startswith("/v1/"):
        return "openai", f"{_OPENAI_BASE}{path}"
    return None


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class Gateway:
    """LLM API reverse proxy with credential isolation.

    Attributes:
        port: TCP port the gateway listens on.
        host: Bind address (``0.0.0.0`` for containers to reach).
        container_host: Hostname containers use (``host.docker.internal``).
        key: Per-session ephemeral key for container authentication.
    """

    def __init__(self, *, port: int, host: str, container_host: str) -> None:
        self.port = port
        self.host = host
        self.container_host = container_host
        self.key: str = f"gw-{secrets.token_urlsafe(32)}"

        self._credentials: dict[str, dict[str, str]] = {}
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """URL that containers use to reach the gateway."""
        return f"http://{self.container_host}:{self.port}"

    def has_provider(self, name: str) -> bool:
        """Return whether the gateway has credentials for *name*."""
        return name in self._credentials

    # ------------------------------------------------------------------
    # Credential discovery
    # ------------------------------------------------------------------

    def _discover_credentials(self) -> None:
        """Collect real API credentials from config and auto-discovery."""
        from pynchy.container_runner._credentials import _read_oauth_token

        s = get_settings()
        providers: dict[str, dict[str, str]] = {}

        # --- Anthropic ---
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

        # --- OpenAI ---
        if s.secrets.openai_api_key:
            providers["openai"] = {
                "type": "api_key",
                "value": s.secrets.openai_api_key.get_secret_value(),
            }

        self._credentials = providers
        logger.info(
            "Gateway credentials discovered",
            providers=list(providers.keys()) or ["none"],
        )

    # ------------------------------------------------------------------
    # Auth validation
    # ------------------------------------------------------------------

    def _validate_auth(self, request: web.Request) -> bool:
        """Check that the inbound request carries a valid gateway key."""
        auth = request.headers.get("Authorization", "")
        api_key = request.headers.get("X-Api-Key", "")
        return auth == f"Bearer {self.key}" or api_key == self.key

    # ------------------------------------------------------------------
    # Request proxying
    # ------------------------------------------------------------------

    def _build_upstream_headers(self, request: web.Request, provider: str) -> dict[str, str]:
        """Forward all headers except auth-related, then inject real credentials."""
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
        elif provider == "openai":
            headers["Authorization"] = f"Bearer {creds['value']}"

        return headers

    async def _proxy_handler(self, request: web.Request) -> web.StreamResponse:
        """Validate auth, resolve provider, proxy request, stream response."""
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
        """Discover credentials, create server, and start listening."""
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
            "LLM gateway listening",
            port=self.port,
            container_url=self.base_url,
            providers=list(self._credentials.keys()),
        )

    async def stop(self) -> None:
        """Graceful shutdown — close connections and stop server."""
        if self._session:
            await self._session.close()
            self._session = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("LLM gateway stopped")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_gateway: Gateway | None = None


def get_gateway() -> Gateway | None:
    """Return the active gateway, or ``None`` if not started."""
    return _gateway


async def start_gateway() -> Gateway:
    """Start the LLM gateway. Returns the instance."""
    global _gateway
    s = get_settings()

    _gateway = Gateway(
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
