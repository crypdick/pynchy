"""Fail-closed policy and listener setup for the HTTP control plane."""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NewType

from aiohttp import web

from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.logger import logger

if TYPE_CHECKING:
    from aiohttp.typedefs import Handler as AiohttpHandler
    from aiohttp.typedefs import Middleware as AiohttpMiddleware
    from aiohttp.web_app import Application as AiohttpApplication
else:
    AiohttpHandler = object
    AiohttpMiddleware = object
    AiohttpApplication = object

ControlPlaneToken = NewType("ControlPlaneToken", str)
ClientAddress = NewType("ClientAddress", str)

MINIMUM_TOKEN_LENGTH = 32
READINESS_PATH = "/health"
_MAX_UNIX_SOCKET_PATH_BYTES = 100
_AUDIT_CHAT_JID = "control-plane"
_AUDIT_WORKSPACE = "host"


class ControlPlaneConfigurationError(ValueError):
    """The configured control-plane posture cannot start safely."""


@dataclass(frozen=True)
class RateLimitDecision:
    """Result of consuming one request from a client's fixed window."""

    allowed: bool
    retry_after_seconds: int = 0


@dataclass
class _RateWindow:
    started_at: float
    request_count: int


class RequestRateLimiter:
    """Per-client fixed-window limiter for remotely reachable requests."""

    def __init__(self, *, request_limit: int, window_seconds: int) -> None:
        self._request_limit = request_limit
        self._window_seconds = window_seconds
        self._windows: dict[ClientAddress, _RateWindow] = {}

    def consume(self, client: ClientAddress, *, now: float | None = None) -> RateLimitDecision:
        current_time = time.monotonic() if now is None else now
        window = self._windows.get(client)
        if window is None or current_time - window.started_at >= self._window_seconds:
            self._windows[client] = _RateWindow(started_at=current_time, request_count=1)
            self._prune_expired_windows(current_time)
            return RateLimitDecision(allowed=True)

        if window.request_count >= self._request_limit:
            remaining = self._window_seconds - (current_time - window.started_at)
            return RateLimitDecision(allowed=False, retry_after_seconds=max(1, int(remaining) + 1))

        window.request_count += 1
        return RateLimitDecision(allowed=True)

    def _prune_expired_windows(self, now: float) -> None:
        if len(self._windows) < 1024:
            return
        self._windows = {
            client: window
            for client, window in self._windows.items()
            if now - window.started_at < self._window_seconds
        }


@dataclass(frozen=True)
class ControlPlaneRuntime:
    """Parsed listener and authorization state used throughout one process."""

    bind_host: str
    port: int
    unix_socket: Path | None
    public_bind: bool
    remote_auth_required: bool
    allow_remote_deploy: bool
    auth_token: ControlPlaneToken | None = field(repr=False)
    rate_limiter: RequestRateLimiter = field(repr=False, compare=False)
    unix_socket_bind: str | None = None


def _resolved_path(project_root: Path, path: Path | None) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else (project_root / path).resolve()


def _unix_socket_paths(
    project_root: Path,
    configured_path: Path | None,
) -> tuple[Path | None, str | None]:
    socket_path = _resolved_path(project_root, configured_path)
    if socket_path is None:
        return None, None
    candidates = (str(socket_path), os.path.relpath(socket_path, start=Path.cwd()))
    bind_path = min(candidates, key=lambda candidate: len(os.fsencode(candidate)))
    if len(os.fsencode(bind_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
        raise ControlPlaneConfigurationError(
            "Control-plane Unix socket path exceeds the portable length limit; "
            "configure server.unix_socket with a shorter path"
        )
    return socket_path, bind_path


def is_loopback_bind_host(host: str) -> bool:
    """Return whether a bind address proves that only the local host can connect."""
    normalized = host.strip().lower().removeprefix("[").removesuffix("]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _read_control_plane_token(
    *,
    auth_token_env: str,
    auth_token_file: Path | None,
    project_root: Path,
) -> ControlPlaneToken | None:
    if env_token := os.environ.get(auth_token_env):
        return ControlPlaneToken(env_token)

    token_path = _resolved_path(project_root, auth_token_file)
    if token_path is None or not token_path.exists():
        return None
    token_stat = token_path.stat()
    if not stat.S_ISREG(token_stat.st_mode):
        raise ControlPlaneConfigurationError(
            f"Control-plane token path is not a regular file: {token_path}"
        )
    if stat.S_IMODE(token_stat.st_mode) & 0o077:
        raise ControlPlaneConfigurationError(
            f"Control-plane token file must have mode 0600: {token_path}"
        )
    token = token_path.read_text(encoding="utf-8").strip()
    return ControlPlaneToken(token) if token else None


def resolve_control_plane_runtime(  # noqa: PLR0913, RUF100 - composition passes each validated listener setting directly.
    *,
    bind_host: str,
    port: int,
    unix_socket: Path | None,
    allow_public_bind: bool,
    allow_remote_deploy: bool,
    auth_token_env: str,
    auth_token_file: Path | None,
    rate_limit_requests: int,
    rate_limit_window_seconds: int,
    project_root: Path,
) -> ControlPlaneRuntime:
    """Parse settings into a safe runtime posture or refuse startup."""
    public_bind = not is_loopback_bind_host(bind_host)
    if public_bind and not allow_public_bind:
        raise ControlPlaneConfigurationError(
            f"Refusing non-loopback control-plane bind {bind_host!r}; "
            "set server.allow_public_bind=true only with bearer authentication"
        )

    remote_auth_required = allow_public_bind or allow_remote_deploy
    token = _read_control_plane_token(
        auth_token_env=auth_token_env,
        auth_token_file=auth_token_file,
        project_root=project_root,
    )
    if remote_auth_required and token is None:
        raise ControlPlaneConfigurationError(
            "Remote control-plane access requires a bearer token; run "
            "`pynchy control-plane bootstrap` or set the configured auth_token_env"
        )
    if token is not None and len(token.encode()) < MINIMUM_TOKEN_LENGTH:
        raise ControlPlaneConfigurationError(
            f"Control-plane bearer tokens must contain at least {MINIMUM_TOKEN_LENGTH} bytes"
        )

    resolved_unix_socket = None
    unix_socket_bind = None
    if os.name != "nt":
        resolved_unix_socket, unix_socket_bind = _unix_socket_paths(project_root, unix_socket)

    return ControlPlaneRuntime(
        bind_host=bind_host,
        port=port,
        unix_socket=resolved_unix_socket,
        public_bind=public_bind,
        remote_auth_required=remote_auth_required,
        allow_remote_deploy=allow_remote_deploy,
        auth_token=token,
        rate_limiter=RequestRateLimiter(
            request_limit=rate_limit_requests,
            window_seconds=rate_limit_window_seconds,
        ),
        unix_socket_bind=unix_socket_bind,
    )


def bootstrap_control_plane_token(
    *,
    auth_token_file: Path | None,
    project_root: Path,
    rotate: bool,
) -> Path:
    """Create or atomically rotate the permission-restricted bearer token file."""
    token_path = _resolved_path(project_root, auth_token_file)
    if token_path is None:
        raise ControlPlaneConfigurationError(
            "server.auth_token_file must be configured before bootstrapping a token"
        )
    if token_path.exists() and not rotate:
        raise FileExistsError(f"Control-plane token already exists: {token_path}")

    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = token_path.with_name(f".{token_path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        token_file.write(f"{secrets.token_urlsafe(32)}\n")
    temporary_path.replace(token_path)
    token_path.chmod(0o600)
    return token_path


def load_control_plane_client_token(
    *,
    token_env: str,
    token_file: Path | None,
) -> ControlPlaneToken | None:
    """Load a client token without accepting a group/world-readable secret file."""
    return _read_control_plane_token(
        auth_token_env=token_env,
        auth_token_file=token_file,
        project_root=Path.cwd(),
    )


def _request_uses_unix_socket(request: web.Request) -> bool:
    transport = request.transport
    if transport is None:
        return False
    request_socket = transport.get_extra_info("socket")
    return request_socket is not None and request_socket.family == socket.AF_UNIX


def _client_address(request: web.Request) -> ClientAddress:
    transport = request.transport
    if transport is None:
        return ClientAddress("unknown")
    peer_name = transport.get_extra_info("peername")
    if isinstance(peer_name, tuple) and peer_name:
        return ClientAddress(str(peer_name[0]))
    return ClientAddress(str(peer_name or "unknown"))


def _has_valid_bearer_token(request: web.Request, expected: ControlPlaneToken) -> bool:
    scheme, separator, supplied = request.headers.get("Authorization", "").partition(" ")
    return separator == " " and scheme == "Bearer" and hmac.compare_digest(supplied, expected)


async def _audit_request(
    request: web.Request,
    *,
    client: ClientAddress,
    decision: str,
    reason: str,
) -> None:
    request_id = f"http-{secrets.token_urlsafe(12)}"
    logger.info(
        "Control-plane policy decision",
        request_id=request_id,
        client=str(client),
        method=request.method,
        path=request.path,
        decision=decision,
        reason=reason,
    )
    await record_security_event(
        chat_jid=_AUDIT_CHAT_JID,
        workspace=_AUDIT_WORKSPACE,
        tool_name=f"http:{request.method}:{request.path}",
        decision=decision,
        reason=f"client={client}; {reason}",
        request_id=request_id,
    )


def build_control_plane_middleware(
    runtime: ControlPlaneRuntime,
    *,
    provider_authenticated_paths: frozenset[str] = frozenset(),
) -> AiohttpMiddleware:
    """Build middleware that authenticates and audits remotely reachable requests."""

    @web.middleware
    async def control_plane_middleware(
        request: web.Request,
        handler: AiohttpHandler,
    ) -> web.StreamResponse:
        if request.path == READINESS_PATH or _request_uses_unix_socket(request):
            return await handler(request)

        client = _client_address(request)
        if runtime.remote_auth_required:
            rate_decision = runtime.rate_limiter.consume(client)
            if not rate_decision.allowed:
                await _audit_request(
                    request,
                    client=client,
                    decision="rate_limited",
                    reason="remote request rate limit exceeded",
                )
                return web.json_response(
                    {"error": "rate limit exceeded"},
                    status=429,
                    headers={"Retry-After": str(rate_decision.retry_after_seconds)},
                )

            token = runtime.auth_token
            if token is None or not _has_valid_bearer_token(request, token):
                if request.method == "POST" and request.path in provider_authenticated_paths:
                    return await handler(request)
                await _audit_request(
                    request,
                    client=client,
                    decision="denied",
                    reason="missing or invalid bearer token",
                )
                return web.json_response(
                    {"error": "authentication required"},
                    status=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )

        if request.path == "/deploy" and not runtime.allow_remote_deploy:
            await _audit_request(
                request,
                client=client,
                decision="denied",
                reason="remote deploy is disabled",
            )
            return web.json_response({"error": "remote deploy is disabled"}, status=403)

        if runtime.remote_auth_required:
            await _audit_request(
                request,
                client=client,
                decision="allowed",
                reason="bearer authentication accepted",
            )
        return await handler(request)

    return control_plane_middleware


def _remove_socket_if_owned(socket_path: Path) -> None:
    if not socket_path.exists():
        return
    if not stat.S_ISSOCK(socket_path.stat().st_mode):
        raise ControlPlaneConfigurationError(
            f"Refusing to replace non-socket control-plane path: {socket_path}"
        )
    socket_path.unlink()


def register_unix_socket_cleanup(app: AiohttpApplication, runtime: ControlPlaneRuntime) -> None:
    """Remove only the Unix socket owned by this server during cleanup."""
    socket_path = runtime.unix_socket
    if socket_path is None:
        return

    async def cleanup_unix_socket(_app: AiohttpApplication) -> None:  # noqa: RUF029, RUF100 - aiohttp cleanup callbacks are async.
        _remove_socket_if_owned(socket_path)

    app.on_cleanup.append(cleanup_unix_socket)


async def start_control_plane_sites(runner: web.AppRunner, runtime: ControlPlaneRuntime) -> None:
    """Start the portable TCP listener and preferred local Unix listener."""
    if runtime.unix_socket is not None:
        runtime.unix_socket.parent.mkdir(parents=True, exist_ok=True)
        _remove_socket_if_owned(runtime.unix_socket)

    tcp_site = web.TCPSite(runner, runtime.bind_host, runtime.port)
    await tcp_site.start()

    if runtime.unix_socket is None:
        return
    unix_site = web.UnixSite(runner, runtime.unix_socket_bind or str(runtime.unix_socket))
    await unix_site.start()
    runtime.unix_socket.chmod(0o600)
