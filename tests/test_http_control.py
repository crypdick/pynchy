"""Behavioral tests for the fail-closed HTTP control-plane policy."""

from __future__ import annotations

import socket
import stat
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, make_mocked_request

import pynchy.host.orchestrator.http_control as http_control
from pynchy.config.api import ServerConfig
from pynchy.host.orchestrator.http_control import (
    ClientAddress,
    ControlPlaneConfigurationError,
    ControlPlaneRuntime,
    ControlPlaneToken,
    RequestRateLimiter,
    bootstrap_control_plane_token,
    build_control_plane_middleware,
    register_unix_socket_cleanup,
    resolve_control_plane_runtime,
    start_control_plane_sites,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


TEST_TOKEN = "control-plane-test-token-value-000000"  # noqa: S105 - synthetic bearer fixture, not a credential.
PUBLIC_BIND_TEST_HOST = "0.0.0.0"  # noqa: S104 - test data for explicit public-bind policy.


@pytest.fixture
def short_unix_socket_root() -> Iterator[Path]:
    """Yield a short root for tests that bind a real Unix socket."""
    with TemporaryDirectory(prefix="pynchy-control-", dir="/tmp") as root:
        yield Path(root).resolve()


async def _discard_audit(
    *_args: object, **_kwargs: object
) -> None:  # middleware requires an awaitable audit callback.
    pass


def _runtime(server: ServerConfig, *, project_root: Path) -> ControlPlaneRuntime:
    return resolve_control_plane_runtime(
        bind_host=server.host,
        port=server.port,
        unix_socket=server.unix_socket,
        allow_public_bind=server.allow_public_bind,
        allow_remote_deploy=server.allow_remote_deploy,
        auth_token_env=server.auth_token_env,
        auth_token_file=server.auth_token_file,
        rate_limit_requests=server.rate_limit_requests,
        rate_limit_window_seconds=server.rate_limit_window_seconds,
        project_root=project_root,
        audit_security_event=_discard_audit,
    )


def test_default_runtime_binds_loopback_and_enables_unix_socket(
    short_unix_socket_root: Path,
) -> None:
    runtime = _runtime(ServerConfig(), project_root=short_unix_socket_root)

    assert runtime.bind_host == "127.0.0.1"
    assert runtime.public_bind is False
    assert runtime.remote_auth_required is False
    assert runtime.unix_socket == short_unix_socket_root / "data" / "pynchy.sock"
    assert runtime.unix_socket_bind is not None


def test_windows_runtime_skips_unix_socket_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_control.os, "name", "nt")

    runtime = _runtime(ServerConfig(unix_socket=None), project_root=tmp_path)

    assert runtime.unix_socket is None
    assert runtime.unix_socket_bind is None


def test_runtime_rejects_an_overlong_unix_socket_path(tmp_path: Path) -> None:
    with pytest.raises(ControlPlaneConfigurationError, match="portable length limit"):
        _runtime(
            ServerConfig(unix_socket=Path("socket-" + "x" * 101)),
            project_root=tmp_path,
        )


def test_non_loopback_bind_requires_explicit_public_opt_in(tmp_path: Path) -> None:
    server = ServerConfig(host=PUBLIC_BIND_TEST_HOST)

    with pytest.raises(ControlPlaneConfigurationError, match="allow_public_bind"):
        _runtime(server, project_root=tmp_path)


@pytest.mark.parametrize(
    ("allow_public_bind", "allow_remote_deploy"),
    [(True, False), (False, True)],
)
def test_each_remote_posture_requires_authentication(
    tmp_path: Path,
    *,
    allow_public_bind: bool,
    allow_remote_deploy: bool,
) -> None:
    server = ServerConfig(
        allow_public_bind=allow_public_bind,
        allow_remote_deploy=allow_remote_deploy,
    )

    with pytest.raises(ControlPlaneConfigurationError, match="requires a bearer token"):
        _runtime(server, project_root=tmp_path)


def test_public_runtime_accepts_strong_environment_token(
    short_unix_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYNCHY_CONTROL_TOKEN", TEST_TOKEN)
    server = ServerConfig(host=PUBLIC_BIND_TEST_HOST, allow_public_bind=True)

    runtime = _runtime(server, project_root=short_unix_socket_root)

    assert runtime.public_bind is True
    assert runtime.remote_auth_required is True


def test_remote_posture_rejects_short_bearer_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYNCHY_CONTROL_TOKEN", "too-short")

    with pytest.raises(ControlPlaneConfigurationError, match="at least 32 bytes"):
        _runtime(ServerConfig(allow_remote_deploy=True), project_root=tmp_path)


def test_token_file_must_be_permission_restricted(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text(TEST_TOKEN)
    token_path.chmod(0o644)
    server = ServerConfig(
        allow_remote_deploy=True,
        auth_token_file=token_path,
    )

    with pytest.raises(ControlPlaneConfigurationError, match="mode 0600"):
        _runtime(server, project_root=tmp_path)


def test_bootstrap_creates_and_rotates_mode_0600_token(tmp_path: Path) -> None:
    server = ServerConfig(auth_token_file=Path("secrets/control.token"))

    token_path = bootstrap_control_plane_token(
        auth_token_file=server.auth_token_file,
        project_root=tmp_path,
        rotate=False,
    )
    first_token = token_path.read_text()

    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert len(first_token.strip()) >= 32
    with pytest.raises(FileExistsError, match="already exists"):
        bootstrap_control_plane_token(
            auth_token_file=server.auth_token_file,
            project_root=tmp_path,
            rotate=False,
        )

    bootstrap_control_plane_token(
        auth_token_file=server.auth_token_file,
        project_root=tmp_path,
        rotate=True,
    )
    assert token_path.read_text() != first_token
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_rate_limiter_resets_after_its_window() -> None:
    limiter = RequestRateLimiter(request_limit=1, window_seconds=10)
    client = ClientAddress("203.0.113.7")

    assert limiter.consume(client, now=1.0).allowed is True
    denied = limiter.consume(client, now=2.0)
    assert denied.allowed is False
    assert denied.retry_after_seconds == 10
    assert limiter.consume(client, now=11.0).allowed is True


def test_rate_limiter_prunes_large_expired_window_map() -> None:
    limiter = RequestRateLimiter(request_limit=2, window_seconds=10)

    for index in range(1024):
        assert limiter.consume(ClientAddress(str(index)), now=1.0).allowed is True

    assert limiter.consume(ClientAddress("new"), now=1.0).allowed is True


class TestRemoteControlPlanePolicy(AioHTTPTestCase):
    """Exercise policy middleware over a real TCP test listener."""

    async def get_application(self) -> web.Application:  # noqa: V105
        runtime = ControlPlaneRuntime(
            bind_host=PUBLIC_BIND_TEST_HOST,
            port=8484,
            unix_socket=None,
            public_bind=True,
            remote_auth_required=True,
            allow_remote_deploy=False,
            auth_token=ControlPlaneToken(TEST_TOKEN),
            rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
            audit_security_event=self.audit,
        )
        app = web.Application(
            middlewares=[
                build_control_plane_middleware(
                    runtime,
                    provider_authenticated_paths=frozenset({"/webhooks/example/project"}),
                )
            ]
        )
        app.router.add_get("/health", self._ok)
        app.router.add_get("/status", self._ok)
        app.router.add_post("/deploy", self._ok)
        app.router.add_post("/webhooks/example/project", self._ok)
        app.router.add_get("/webhooks/example/project", self._ok)
        return app

    async def asyncSetUp(self) -> None:
        self.audit = AsyncMock()
        await super().asyncSetUp()

    @staticmethod
    async def _ok(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def test_readiness_stays_public_and_unaudited(self) -> None:
        response = await self.client.get("/health")

        assert response.status == 200
        assert await response.json() == {"status": "ok"}
        self.audit.assert_not_awaited()

    async def test_remote_operator_endpoint_rejects_missing_or_invalid_bearer(self) -> None:
        missing = await self.client.get("/status")
        invalid = await self.client.get(
            "/status",
            headers={"Authorization": "Bearer invalid"},
        )

        assert missing.status == 401
        assert missing.headers["WWW-Authenticate"] == "Bearer"
        assert invalid.status == 401
        assert [call.kwargs["decision"] for call in self.audit.await_args_list] == [
            "denied",
            "denied",
        ]

    async def test_remote_operator_endpoint_accepts_and_audits_valid_bearer(self) -> None:
        response = await self.client.get(
            "/status",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )

        assert response.status == 200
        self.audit.assert_awaited_once()
        assert self.audit.await_args.kwargs["decision"] == "allowed"
        assert self.audit.await_args.kwargs["tool_name"] == "http:GET:/status"

    async def test_public_bind_does_not_implicitly_enable_remote_deploy(self) -> None:
        response = await self.client.post(
            "/deploy",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )

        assert response.status == 403
        assert await response.json() == {"error": "remote deploy is disabled"}
        assert self.audit.await_args.kwargs["decision"] == "denied"

    async def test_only_registered_webhook_post_bypasses_bearer(self) -> None:
        provider_post = await self.client.post("/webhooks/example/project")
        same_path_get = await self.client.get("/webhooks/example/project")

        assert provider_post.status == 200
        assert same_path_get.status == 401


class TestLoopbackDeployPolicy(AioHTTPTestCase):
    """Prove loopback TCP cannot masquerade as the local Unix control path."""

    async def get_application(self) -> web.Application:  # noqa: V105
        runtime = ControlPlaneRuntime(
            bind_host="127.0.0.1",
            port=8484,
            unix_socket=None,
            public_bind=False,
            remote_auth_required=False,
            allow_remote_deploy=False,
            auth_token=None,
            rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
            audit_security_event=_discard_audit,
        )
        app = web.Application(middlewares=[build_control_plane_middleware(runtime)])
        app.router.add_post("/deploy", self._ok)
        return app

    @staticmethod
    async def _ok(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def test_loopback_tcp_deploy_requires_explicit_opt_in(self) -> None:
        response = await self.client.post("/deploy")

        assert response.status == 403


class TestRemoteRateLimit(AioHTTPTestCase):
    """Prove unauthorized requests consume the remote request budget."""

    async def get_application(self) -> web.Application:  # noqa: V105
        runtime = ControlPlaneRuntime(
            bind_host=PUBLIC_BIND_TEST_HOST,
            port=8484,
            unix_socket=None,
            public_bind=True,
            remote_auth_required=True,
            allow_remote_deploy=True,
            auth_token=ControlPlaneToken(TEST_TOKEN),
            rate_limiter=RequestRateLimiter(request_limit=1, window_seconds=60),
            audit_security_event=_discard_audit,
        )
        app = web.Application(middlewares=[build_control_plane_middleware(runtime)])
        app.router.add_get("/status", self._ok)
        return app

    @staticmethod
    async def _ok(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def test_rate_limit_runs_before_bearer_acceptance(self) -> None:
        unauthorized = await self.client.get("/status")
        limited = await self.client.get(
            "/status",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )

        assert unauthorized.status == 401
        assert limited.status == 429
        assert int(limited.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_unix_socket_is_mode_0600_and_bypasses_tcp_bearer(
    short_unix_socket_root: Path,
) -> None:
    socket_path = short_unix_socket_root / "control.sock"
    audit = AsyncMock()
    runtime = ControlPlaneRuntime(
        bind_host="127.0.0.1",
        port=0,
        unix_socket=socket_path,
        public_bind=False,
        remote_auth_required=True,
        allow_remote_deploy=True,
        auth_token=ControlPlaneToken(TEST_TOKEN),
        rate_limiter=RequestRateLimiter(request_limit=1, window_seconds=60),
        audit_security_event=audit,
    )

    async def ok(_request: web.Request) -> web.Response:  # noqa: RUF029 - aiohttp route handlers are async.
        return web.json_response({"status": "ok"})

    app = web.Application(middlewares=[build_control_plane_middleware(runtime)])
    app.router.add_get("/status", ok)
    register_unix_socket_cleanup(app, runtime)
    runner = web.AppRunner(app)
    await runner.setup()
    await start_control_plane_sites(runner, runtime)
    try:
        connector = aiohttp.UnixConnector(path=str(socket_path))
        async with (
            aiohttp.ClientSession(connector=connector) as session,
            session.get("http://localhost/status") as response,
        ):
            assert response.status == 200
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        audit.assert_not_awaited()
    finally:
        await runner.cleanup()

    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_request_without_transport_still_requires_remote_auth() -> None:
    runtime = ControlPlaneRuntime(
        bind_host=PUBLIC_BIND_TEST_HOST,
        port=8484,
        unix_socket=None,
        public_bind=True,
        remote_auth_required=True,
        allow_remote_deploy=True,
        auth_token=ControlPlaneToken(TEST_TOKEN),
        rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
        audit_security_event=_discard_audit,
    )
    middleware = build_control_plane_middleware(runtime)
    request = make_mocked_request("GET", "/status")
    request._protocol.transport = None

    unexpected_handler = AsyncMock(side_effect=AssertionError("request should be denied"))

    response = await middleware(request, unexpected_handler)

    assert response.status == 401


@pytest.mark.asyncio
async def test_request_uses_tuple_peer_address_for_remote_auth() -> None:
    runtime = ControlPlaneRuntime(
        bind_host=PUBLIC_BIND_TEST_HOST,
        port=8484,
        unix_socket=None,
        public_bind=True,
        remote_auth_required=True,
        allow_remote_deploy=True,
        auth_token=ControlPlaneToken(TEST_TOKEN),
        rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
        audit_security_event=_discard_audit,
    )
    middleware = build_control_plane_middleware(runtime)
    request = make_mocked_request("GET", "/status")
    request._protocol.transport = MagicMock()
    request._protocol.transport.get_extra_info.side_effect = lambda name: (
        None if name == "socket" else ("203.0.113.7", 8484)
    )

    response = await middleware(request, AsyncMock())

    assert response.status == 401


@pytest.mark.asyncio
async def test_request_uses_unknown_address_for_non_tuple_peer() -> None:
    runtime = ControlPlaneRuntime(
        bind_host=PUBLIC_BIND_TEST_HOST,
        port=8484,
        unix_socket=None,
        public_bind=True,
        remote_auth_required=True,
        allow_remote_deploy=True,
        auth_token=ControlPlaneToken(TEST_TOKEN),
        rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
        audit_security_event=_discard_audit,
    )
    middleware = build_control_plane_middleware(runtime)
    request = make_mocked_request("GET", "/status")
    request._protocol.transport = MagicMock()
    request._protocol.transport.get_extra_info.side_effect = lambda name: (
        None if name == "socket" else "not-a-peer-tuple"
    )

    response = await middleware(request, AsyncMock())

    assert response.status == 401


@pytest.mark.asyncio
async def test_start_replaces_a_stale_unix_socket(short_unix_socket_root: Path) -> None:
    runtime = _runtime(
        ServerConfig(unix_socket=Path("control.sock")),
        project_root=short_unix_socket_root,
    )
    socket_path = runtime.unix_socket
    assert socket_path is not None
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    stale_socket = socket.socket(socket.AF_UNIX)
    stale_socket.bind(str(socket_path))
    stale_socket.close()
    assert stat.S_ISSOCK(socket_path.stat().st_mode)

    runner = web.AppRunner(web.Application())
    await runner.setup()
    try:
        await start_control_plane_sites(runner, runtime)
        assert stat.S_ISSOCK(socket_path.stat().st_mode)
    finally:
        await runner.cleanup()

    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_deep_project_root_binds_unix_socket_through_short_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path.joinpath(*(["deep-project-directory"] * 6))
    project_root.mkdir(parents=True)
    monkeypatch.chdir(project_root)
    runtime = _runtime(ServerConfig(), project_root=project_root)
    runtime = replace(runtime, port=0)

    assert runtime.unix_socket is not None
    assert len(str(runtime.unix_socket).encode()) > 100
    assert runtime.unix_socket_bind == "data/pynchy.sock"

    app = web.Application()
    register_unix_socket_cleanup(app, runtime)
    runner = web.AppRunner(app)
    await runner.setup()
    await start_control_plane_sites(runner, runtime)
    try:
        assert runtime.unix_socket.exists()
        assert stat.S_IMODE(runtime.unix_socket.stat().st_mode) == 0o600
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_start_control_plane_rejects_replacing_a_regular_file(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    socket_path.write_text("not a socket")
    runtime = ControlPlaneRuntime(
        bind_host="127.0.0.1",
        port=0,
        unix_socket=socket_path,
        public_bind=False,
        remote_auth_required=False,
        allow_remote_deploy=False,
        auth_token=None,
        rate_limiter=RequestRateLimiter(request_limit=1, window_seconds=60),
        audit_security_event=_discard_audit,
    )
    runner = web.AppRunner(web.Application())
    await runner.setup()
    try:
        with pytest.raises(ControlPlaneConfigurationError, match="non-socket"):
            await start_control_plane_sites(runner, runtime)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_start_control_plane_supports_tcp_without_unix_socket() -> None:
    runtime = ControlPlaneRuntime(
        bind_host="127.0.0.1",
        port=0,
        unix_socket=None,
        public_bind=False,
        remote_auth_required=False,
        allow_remote_deploy=False,
        auth_token=None,
        rate_limiter=RequestRateLimiter(request_limit=1, window_seconds=60),
        audit_security_event=_discard_audit,
    )
    runner = web.AppRunner(web.Application())
    await runner.setup()
    try:
        await start_control_plane_sites(runner, runtime)
        assert runner.addresses
    finally:
        await runner.cleanup()
