"""Builtin gateway — aiohttp reverse proxy for single-key setups.

Used when ``litellm_config`` is not set in config.toml.  Reads keys from
``[secrets]``.  Containers get the same env vars as LiteLLM mode
(``ANTHROPIC_BASE_URL``, ``OPENAI_BASE_URL``, etc.) so they work without
URL changes.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping

import aiohttp
from aiohttp import web

from pynchy.config import get_settings
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ANTHROPIC_BASE = "https://api.anthropic.com"
_OPENAI_BASE = "https://api.openai.com"

_STRIP_REQUEST_HEADERS = frozenset({"authorization", "x-api-key", "host", "content-length"})
_STRIP_RESPONSE_HEADERS = frozenset(
    {"transfer-encoding", "content-encoding", "connection", "keep-alive"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_provider(path: str) -> tuple[str, str] | None:
    """Map request path to ``(provider_name, upstream_url)``."""
    if path.startswith("/v1/messages"):
        return "anthropic", f"{_ANTHROPIC_BASE}{path}"
    if path.startswith("/v1/"):
        return "openai", f"{_OPENAI_BASE}{path}"
    return None


# ---------------------------------------------------------------------------
# BuiltinGateway
# ---------------------------------------------------------------------------


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
        s = get_settings()
        providers: dict[str, dict[str, str]] = {}

        if s.secrets.anthropic_api_key:
            providers["anthropic"] = {
                "type": "api_key",
                "value": s.secrets.anthropic_api_key.get_secret_value(),
            }

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

    def _validate_auth(self, headers: Mapping[str, str]) -> bool:
        auth = headers.get("Authorization", "")
        api_key = headers.get("X-Api-Key", "")
        return auth == f"Bearer {self.key}" or api_key == self.key

    def _build_upstream_headers(
        self, headers_in: Mapping[str, str], provider: str
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in headers_in.items():
            if key.lower() not in _STRIP_REQUEST_HEADERS:
                headers[key] = value

        creds = self._credentials[provider]
        if provider == "anthropic":
            headers["x-api-key"] = creds["value"]
        elif provider == "openai":
            headers["Authorization"] = f"Bearer {creds['value']}"

        return headers

    # Avoid annotating this as web.Request: beartype inspects aiohttp's
    # typing.MutableMapping base and emits a PEP 585 warning.
    async def _proxy_handler(self, request) -> web.StreamResponse:
        path = f"/{request.match_info.get('path', '')}"

        if not self._validate_auth(request.headers):
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

        headers = self._build_upstream_headers(request.headers, provider)
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
                "Configure [secrets].openai_api_key or [secrets].anthropic_api_key in config.toml."
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
