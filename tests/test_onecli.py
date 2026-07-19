"""Tests for OneCLI Agent Vault container materialization."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from conftest import make_settings

from pynchy.config.models import OneCliConfig
from pynchy.host.container_manager.onecli import (
    OneCliClient,
    OneCliError,
    collect_onecli_status,
    normalize_agent_identifier,
    prepare_onecli_material,
    sync_onecli_gateway_skill,
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


def _onecli_material_payload(ca_container_path: str) -> dict[str, object]:
    return {
        "env": {
            "HTTPS_PROXY": "http://proxy",
            "SSL_CERT_FILE": ca_container_path,
        },
        "caCertificate": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n",
        "caCertificateContainerPath": ca_container_path,
        "credentialStubs": [
            {
                "containerPath": "/home/agent/.codex/auth.json",
                "content": '{"token":"onecli-managed"}',
            }
        ],
        "warnings": ["connected"],
    }


def _mount_for_container_path(material, container_path: str):
    return next(m for m in material.mounts if m.container_path == container_path)


def test_normalize_agent_identifier_lowercases_and_collapses_separators() -> None:
    assert normalize_agent_identifier("pynchy", "Research Group!") == "pynchy-research-group"


def test_prepare_onecli_material_returns_none_when_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)

    with patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings):
        assert prepare_onecli_material("research") is None


def test_collect_onecli_status_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)

    with patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings):
        assert collect_onecli_status() == {
            "enabled": False,
            "url": "http://localhost:10254",
            "fail_closed": True,
        }


def test_collect_onecli_status_reports_health_and_egress_approvals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("ONECLI_API_KEY", "oc_test_key")
    responses = [
        _FakeResponse({"status": "ok", "version": "1.2.3"}),
        _FakeResponse({"requests": [{"id": "approval-1"}, {"id": "approval-2"}]}),
    ]
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return responses.pop(0)

    with (
        patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings),
        patch("pynchy.host.container_manager.onecli.urlopen", side_effect=fake_urlopen),
    ):
        status = collect_onecli_status()

    assert status == {
        "enabled": True,
        "url": "http://localhost:10254",
        "fail_closed": True,
        "api_key_configured": True,
        "project_id_configured": False,
        "ready": True,
        "version": "1.2.3",
        "egress_pending_approvals": 2,
    }
    assert requests[0].full_url == "http://localhost:10254/v1/health"
    assert requests[1].full_url == "http://localhost:10254/v1/approvals/pending"


def test_client_fetches_gateway_skill_with_framework() -> None:
    settings = make_settings(onecli=OneCliConfig(url="http://onecli.local"))
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return _FakeRawResponse(b"# OneCLI Gateway\n")

    client = OneCliClient(config=settings.onecli, api_key="oc_test_key", project_id=None)

    with patch("pynchy.host.container_manager.onecli.urlopen", side_effect=fake_urlopen):
        skill = client.get_gateway_skill(agent_framework="claude")

    assert skill == "# OneCLI Gateway\n"
    assert requests[0].full_url == "http://onecli.local/v1/skill/gateway?agent_framework=claude"
    assert requests[0].get_header("Authorization") == "Bearer oc_test_key"


def test_client_rejects_non_http_base_url_before_opening(tmp_path: Path) -> None:
    settings = make_settings(onecli=OneCliConfig(url=(tmp_path / "onecli.sock").as_uri()))
    client = OneCliClient(config=settings.onecli, api_key="oc_test_key", project_id=None)

    with (
        patch("pynchy.host.container_manager.onecli.urlopen") as opener,
        pytest.raises(OneCliError, match="must use http or https"),
    ):
        client.health()

    opener.assert_not_called()


def test_sync_onecli_gateway_skill_writes_generated_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("ONECLI_API_KEY", "oc_test_key")
    skills_dir = tmp_path / "skills"

    with (
        patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.onecli.urlopen",
            return_value=_FakeRawResponse(b"---\nname: onecli-gateway\n---\nUse OneCLI.\n"),
        ),
    ):
        sync_onecli_gateway_skill(skills_dir)

    skill_dir = skills_dir / "onecli-gateway"
    assert (skill_dir / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "---\nname: onecli-gateway\n---\nUse OneCLI.\n"
    assert (skill_dir / ".pynchy-onecli-skill").exists()


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


def test_prepare_onecli_material_resolves_proxy_host_for_apple_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("ONECLI_API_KEY", "oc_test_key")
    payload = {
        "env": {
            "HTTPS_PROXY": "http://host.docker.internal:10255",
            "http_proxy": "http://host.docker.internal:10255",
            "SSL_CERT_FILE": "/opt/onecli-ca.pem",
        },
        "credentialStubs": [],
    }

    with (
        patch("pynchy.host.container_manager.onecli.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.onecli.urlopen",
            return_value=_FakeResponse(payload),
        ),
        patch(
            "pynchy.host.container_manager.onecli.resolve_container_host",
            return_value="192.168.64.1",
        ),
    ):
        material = prepare_onecli_material("research")

    assert material is not None
    assert material.env_vars == {
        "HTTPS_PROXY": "http://192.168.64.1:10255",
        "http_proxy": "http://192.168.64.1:10255",
        "SSL_CERT_FILE": "/opt/onecli-ca.pem",
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
    ca_container_path = str(PurePosixPath("/", "tmp", "onecli-ca.pem"))
    payload = _onecli_material_payload(ca_container_path)
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

    ca_mount = _mount_for_container_path(material, ca_container_path)
    assert ca_mount.readonly is True
    assert Path(ca_mount.host_path).read_text(encoding="utf-8") == payload["caCertificate"]

    stub_mount = _mount_for_container_path(material, "/home/agent/.codex/auth.json")
    assert stub_mount.readonly is True
    assert Path(stub_mount.host_path).read_text(encoding="utf-8") == '{"token":"onecli-managed"}'
