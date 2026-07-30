"""Public OAuth-flow behavior for Google profile setup."""

from __future__ import annotations

import asyncio
import threading
import urllib.request
from typing import TYPE_CHECKING

import pytest

from pynchy.plugins.integrations.google_setup import run_oauth_flow

if TYPE_CHECKING:
    from pathlib import Path


class _OAuthPage:
    def __init__(self) -> None:
        self.url: str | None = None

    async def goto(self, url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"
        self.url = url


class _Response:
    def __init__(self, contents: bytes) -> None:
        self._contents = contents

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._contents


class _CallbackServer:
    def shutdown(self) -> None:
        return None


def _keys_file(tmp_path: Path) -> Path:
    keys_path = tmp_path / "gcp-oauth.keys.json"
    keys_path.write_text(
        '{"installed": {"client_id": "client-id", "client_secret": "client-secret"}}'
    )
    return keys_path


@pytest.mark.asyncio
async def test_public_oauth_flow_rejects_non_client_credentials(tmp_path: Path) -> None:
    keys_path = tmp_path / "gcp-oauth.keys.json"
    keys_path.write_text("{}")

    with pytest.raises(RuntimeError, match="Invalid credentials JSON"):
        await run_oauth_flow(_OAuthPage(), keys_path, "scope-a")


@pytest.mark.asyncio
async def test_public_oauth_flow_exchanges_tokens_and_adds_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response_value = "test-response"
    callback_received = threading.Event()
    callback_received.set()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.start_callback_server",
        lambda: (callback_received, ["authorization-code"], _CallbackServer()),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request: _Response(
            (
                f'{{"access_token": "{response_value}", "refresh_token": "refresh", '
                '"expires_in": 60}'
            ).encode()
        ),
    )

    tokens = await run_oauth_flow(_OAuthPage(), _keys_file(tmp_path), "scope-a")

    assert tokens["access_token"] == response_value
    assert "refresh_token" in tokens
    assert isinstance(tokens["expiry_date"], int)


@pytest.mark.asyncio
async def test_public_oauth_flow_handles_tokens_without_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    callback_received = threading.Event()
    callback_received.set()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.start_callback_server",
        lambda: (callback_received, ["authorization-code"], _CallbackServer()),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request: _Response(b'{"access_token": "access"}'),
    )

    tokens = await run_oauth_flow(_OAuthPage(), _keys_file(tmp_path), "scope-a")

    assert tokens == {"access_token": "access"}


@pytest.mark.asyncio
async def test_public_oauth_flow_reports_token_exchange_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    callback_received = threading.Event()
    callback_received.set()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.start_callback_server",
        lambda: (callback_received, ["authorization-code"], _CallbackServer()),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.urlopen_https_request",
        lambda _request: _Response(b'{"error": "invalid_grant"}'),
    )

    with pytest.raises(RuntimeError, match="Token exchange failed: invalid_grant"):
        await run_oauth_flow(_OAuthPage(), _keys_file(tmp_path), "scope-a")


@pytest.mark.asyncio
async def test_public_oauth_flow_reports_callback_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def wait_for_callback(*_args: object) -> bool:
        await asyncio.sleep(0)
        return False

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.start_callback_server",
        lambda: (threading.Event(), [], _CallbackServer()),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.asyncio.to_thread", wait_for_callback
    )

    with pytest.raises(RuntimeError, match="OAuth callback not received"):
        await run_oauth_flow(_OAuthPage(), _keys_file(tmp_path), "scope-a")


@pytest.mark.asyncio
async def test_public_oauth_flow_rejects_callback_without_authorization_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    callback_received = threading.Event()
    callback_received.set()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.start_callback_server",
        lambda: (callback_received, [], _CallbackServer()),
    )

    with pytest.raises(RuntimeError, match="OAuth callback not received"):
        await run_oauth_flow(_OAuthPage(), _keys_file(tmp_path), "scope-a")
