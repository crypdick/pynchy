"""Fail-closed client token and listener-address behavior."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from pynchy.host.orchestrator.http_control import (
    ControlPlaneConfigurationError,
    bootstrap_control_plane_token,
    is_loopback_bind_host,
    load_control_plane_client_token,
    resolve_control_plane_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path

MISSING_TOKEN_ENV = "MISSING_CONTROL_TOKEN"  # noqa: S105 - synthetic env name.
EMPTY_TOKEN_ENV = "EMPTY_CONTROL_TOKEN"  # noqa: S105 - synthetic env name.


def test_client_token_is_absent_without_environment_or_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_CONTROL_TOKEN", raising=False)

    assert (
        load_control_plane_client_token(
            token_env=MISSING_TOKEN_ENV,
            token_file=tmp_path / "missing-token",
        )
        is None
    )


def test_empty_client_token_file_is_treated_as_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMPTY_CONTROL_TOKEN", raising=False)
    token_file = tmp_path / "empty-token"
    token_file.write_text("\n")
    token_file.chmod(0o600)

    assert (
        load_control_plane_client_token(
            token_env=EMPTY_TOKEN_ENV,
            token_file=token_file,
        )
        is None
    )


def test_client_token_rejects_a_directory_as_a_secret_file(tmp_path: Path) -> None:
    token_dir = tmp_path / "token-dir"
    token_dir.mkdir()

    with pytest.raises(ControlPlaneConfigurationError, match="regular file"):
        load_control_plane_client_token(token_env=MISSING_TOKEN_ENV, token_file=token_dir)


def test_bootstrap_requires_a_configured_token_file(tmp_path: Path) -> None:
    with pytest.raises(ControlPlaneConfigurationError, match="must be configured"):
        bootstrap_control_plane_token(
            auth_token_file=None,
            project_root=tmp_path,
            rotate=False,
        )


async def _discard_audit(*_args: object, **_kwargs: object) -> None:
    await asyncio.sleep(0)


def test_runtime_without_unix_socket_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MISSING_TOKEN_ENV, raising=False)

    runtime = resolve_control_plane_runtime(
        bind_host="127.0.0.1",
        port=8484,
        unix_socket=None,
        allow_public_bind=False,
        allow_remote_deploy=False,
        auth_token_env=MISSING_TOKEN_ENV,
        auth_token_file=None,
        rate_limit_requests=10,
        rate_limit_window_seconds=60,
        project_root=tmp_path,
        audit_security_event=_discard_audit,
    )

    assert runtime.unix_socket is None


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("[::1]", True),
        ("127.0.0.1", True),
        ("203.0.113.7", False),
        ("not-an-ip", False),
    ],
)
def test_loopback_bind_detection_is_fail_closed(host: str, expected: bool) -> None:
    assert is_loopback_bind_host(host) is expected
