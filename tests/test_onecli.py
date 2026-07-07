"""Tests for OneCLI Agent Vault container materialization."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from conftest import make_settings

from pynchy.config.models import OneCliConfig
from pynchy.host.container_manager.onecli import (
    normalize_agent_identifier,
    prepare_onecli_material,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class _FakeRawResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeRawResponse:
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _settings(tmp_path: Path, *, enabled: bool = True) -> object:
    return make_settings(
        data_dir=tmp_path / "data",
        onecli=OneCliConfig(enabled=enabled),
    )


def test_normalize_agent_identifier_lowercases_and_collapses_separators() -> None:
    assert normalize_agent_identifier("pynchy", "Research Group!") == "pynchy-research-group"


def test_prepare_onecli_material_returns_none_when_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)

    with patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings):
        assert prepare_onecli_material("research") is None


def test_prepare_onecli_material_returns_none_when_fail_open_and_key_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = make_settings(
        data_dir=tmp_path / "data",
        onecli=OneCliConfig(enabled=True, fail_closed=False),
    )
    monkeypatch.delenv("ONECLI_API_KEY", raising=False)

    with patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings):
        assert prepare_onecli_material("research") is None


def test_prepare_onecli_material_creates_missing_agent_and_retries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("ONECLI_API_KEY", "oc_test_key")
    payload = {"env": {"HTTPS_PROXY": "http://proxy"}, "credentialStubs": []}
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError(
                url=request.full_url,
                code=404,
                msg="Not Found",
                hdrs={},
                fp=None,
            )
        if request.get_method() == "POST":
            return _FakeResponse({})
        return _FakeResponse(payload)

    with (
        patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings),
        patch("pynchy.host.container_manager.onecli.urlopen", side_effect=fake_urlopen),
    ):
        material = prepare_onecli_material("Research Group!")

    assert material is not None
    assert material.env_vars == {"HTTPS_PROXY": "http://proxy"}
    assert [request.get_method() for request in requests] == ["GET", "POST", "GET"]
    assert json.loads(requests[1].data.decode()) == {
        "name": "Research Group!",
        "identifier": "pynchy-research-group",
    }


def test_prepare_onecli_material_returns_none_when_fail_open_and_invalid_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = make_settings(
        data_dir=tmp_path / "data",
        onecli=OneCliConfig(enabled=True, fail_closed=False),
    )
    monkeypatch.setenv("ONECLI_API_KEY", "oc_test_key")

    with (
        patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.onecli.urlopen",
            return_value=_FakeRawResponse(b"not json"),
        ),
    ):
        assert prepare_onecli_material("research") is None


def test_prepare_onecli_material_writes_ca_and_stubs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("ONECLI_API_KEY", "oc_test_key")
    payload = {
        "env": {
            "HTTPS_PROXY": "http://proxy",
            "SSL_CERT_FILE": "/tmp/onecli-ca.pem",
        },
        "caCertificate": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n",
        "caCertificateContainerPath": "/tmp/onecli-ca.pem",
        "credentialStubs": [
            {
                "containerPath": "/home/agent/.codex/auth.json",
                "content": '{"token":"onecli-managed"}',
            }
        ],
        "warnings": ["connected"],
    }
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return _FakeResponse(payload)

    with (
        patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings),
        patch("pynchy.host.container_manager.onecli.urlopen", side_effect=fake_urlopen),
    ):
        material = prepare_onecli_material("Research Group!")

    assert material is not None
    assert material.env_vars == payload["env"]
    assert material.warnings == ["connected"]
    assert requests[0].full_url.endswith("/v1/container-config?agent=pynchy-research-group")
    assert requests[0].get_header("Authorization") == "Bearer oc_test_key"

    ca_mount = next(m for m in material.mounts if m.container_path == "/tmp/onecli-ca.pem")
    assert ca_mount.readonly is True
    assert Path(ca_mount.host_path).read_text() == payload["caCertificate"]

    stub_mount = next(
        m for m in material.mounts if m.container_path == "/home/agent/.codex/auth.json"
    )
    assert stub_mount.readonly is True
    assert Path(stub_mount.host_path).read_text() == '{"token":"onecli-managed"}'
