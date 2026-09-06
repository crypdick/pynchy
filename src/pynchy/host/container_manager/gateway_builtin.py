"""Builtin gateway — aiohttp reverse proxy for single-key setups.

Used as a component mode when ``litellm_config`` is unset. Normal Pynchy
startup requires personalized LiteLLM configuration. Containers get the same
environment variables as LiteLLM mode
(``ANTHROPIC_BASE_URL``, ``OPENAI_BASE_URL``, etc.) so they work without
URL changes.
"""

from __future__ import annotations

import secrets
from collections.abc import (
    Mapping,
)
from dataclasses import dataclass
from typing import cast

import aiohttp
from aiohttp import web

from pynchy.logger import logger
from pynchy.redaction import (
    GatewayRedactionPosture,
    RedactionRequestError,
    irreversibly_redact_llm_request_body,
    redaction_posture_for_gateway_mode,
)

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


def build_upstream_headers(
    headers_in: Mapping[str, str], provider: str, api_key: str
) -> dict[str, str]:
    """Build provider-native headers without forwarding gateway credentials."""
    headers = {
        key: value for key, value in headers_in.items() if key.lower() not in _STRIP_REQUEST_HEADERS
    }
    if provider == "anthropic":
        headers["x-api-key"] = api_key
    elif provider == "openai":
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _relay_upstream_response(
    *,
    session: aiohttp.ClientSession,
    request: object,
    upstream_url: str,
    headers: dict[str, str],
    body: bytes,
) -> web.StreamResponse:
    proxy_request = cast("web.Request", request)
    async with session.request(
        method=proxy_request.method,
        url=upstream_url,
        headers=headers,
        data=body,
    ) as upstream:
        resp_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _STRIP_RESPONSE_HEADERS
        }

        response = web.StreamResponse(
            status=upstream.status,
            headers=resp_headers,
        )
        await response.prepare(proxy_request)

        async for chunk in upstream.content.iter_any():
            await response.write(chunk)

        await response.write_eof()
        return response


# ---------------------------------------------------------------------------
# BuiltinGateway
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltinGatewayCredentials:
    """Resolved provider keys required by the builtin gateway."""

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None


class BuiltinGateway:
    """Simple aiohttp reverse proxy for single-key setups.

    Used when ``litellm_config`` is not set. Receives resolved credentials
    from gateway composition.
    """

    def __init__(
        self,
        *,
        port: int,
        host: str,
        container_host: str,
        credentials: BuiltinGatewayCredentials | None = None,
    ) -> None:
        self.port = port
        self.host = host
        self.container_host = container_host
        self.key: str = f"gw-{secrets.token_urlsafe(32)}"

        self._configured_credentials = credentials or BuiltinGatewayCredentials()
        self._credentials: dict[str, dict[str, str]] = {}
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.container_host}:{self.port}"

    @property
    def redaction_posture(self) -> GatewayRedactionPosture:
        """Report the request-body redaction enforced by this owned proxy."""
        return redaction_posture_for_gateway_mode("builtin")

    def has_provider(self, name: str) -> bool:
        return name in self._credentials

    def prepare_upstream_body(self, body: bytes) -> bytes:
        """Irreversibly redact one provider request before it leaves Pynchy."""
        return irreversibly_redact_llm_request_body(body)

    # ------------------------------------------------------------------
    # Credential discovery
    # ------------------------------------------------------------------

    def _configure_credentials(self) -> None:
        providers: dict[str, dict[str, str]] = {}

        if self._configured_credentials.anthropic_api_key:
            providers["anthropic"] = {
                "type": "api_key",
                "value": self._configured_credentials.anthropic_api_key,
            }

        if self._configured_credentials.openai_api_key:
            providers["openai"] = {
                "type": "api_key",
                "value": self._configured_credentials.openai_api_key,
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

    # Avoid annotating this as web.Request: beartype inspects aiohttp's
    # typing.MutableMapping base and emits a PEP 585 warning.
    async def _proxy_handler(self, request: object) -> web.StreamResponse:
        proxy_request = cast("web.Request", request)
        path = f"/{proxy_request.match_info.get('path', '')}"

        if not self._validate_auth(proxy_request.headers):
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

        headers = build_upstream_headers(
            proxy_request.headers,
            provider,
            self._credentials[provider]["value"],
        )
        body = await proxy_request.read()
        try:
            upstream_body = self.prepare_upstream_body(body)
        except RedactionRequestError:
            return web.Response(status=400, text="Invalid LLM request body")

        try:
            return await _relay_upstream_response(
                session=cast("aiohttp.ClientSession", self._session),
                request=request,
                upstream_url=upstream_url,
                headers=headers,
                body=upstream_body,
            )
        except aiohttp.ClientError as exc:
            logger.error("Gateway upstream error", provider=provider, err=str(exc))
            return web.Response(status=502, text=f"Gateway error: {type(exc).__name__}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._configure_credentials()

        if not self._credentials:
            logger.warning(
                "Gateway has no LLM credentials — containers will fail to authenticate. "
                "Configure SECRETS__OPENAI_API_KEY or SECRETS__ANTHROPIC_API_KEY in .env."
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
